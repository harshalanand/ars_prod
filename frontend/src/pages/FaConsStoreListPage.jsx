import { useEffect, useMemo, useRef, useState } from 'react'
import { Upload, Download, RefreshCw, Trash2, Plus, X, Search, History, ChevronUp, ChevronDown, Filter, AlertTriangle, CheckCircle2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { faConsAPI } from '@/services/api'
import { C } from '@/theme/colors'
import ColumnFilterPopover, { passColumn, filterActive, matchGlobal, exportCsv } from '@/components/facons/ColumnFilter'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function fmtDate(iso) {
  const m = String(iso || '').slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/)
  return m ? `${m[3]}-${MONTHS[+m[2] - 1]}-${m[1]}` : (iso || '—')
}
function fmtDT(iso) { return iso ? `${fmtDate(iso)} ${String(iso).slice(11, 16)}` : '—' }

const COLS = [
  { key: 'priority', label: '#', num: true, w: 44 },
  { key: 'st_cd', label: 'Store', mono: true, w: 76 },
  { key: 'st_nm', label: 'Store Name', w: 200 },
  { key: 'rdc', label: 'RDC', w: 64 },
  { key: 'hub', label: 'HUB', w: 64 },
  { key: 'op_dt', label: 'OP Date', fmt: fmtDate, w: 100 },
  { key: 'status', label: 'Status', w: 100, badge: true },
  { key: 'remarks', label: 'Remarks', w: 180 },
]
const STATUS_FILTERS = [{ key: 'ALL', label: 'All' }, { key: 'UPC', label: 'UPC' }, { key: 'OLD', label: 'Old' }]

const statusColor = (s) => s === 'OLD' ? { bg: C.greenBg, bd: C.greenBd, fg: C.green }
  : s === 'UPC' ? { bg: C.blueBg, bd: C.blueBd, fg: C.blue }
    : { bg: C.redBg, bd: C.redBd, fg: C.red }

function saveBlob(res, fallback) {
  const url = window.URL.createObjectURL(new Blob([res.data]))
  const a = document.createElement('a'); a.href = url; a.download = fallback
  document.body.appendChild(a); a.click(); a.remove(); window.URL.revokeObjectURL(url)
}

function HistoryModal({ st_cd, onClose }) {
  const [events, setEvents] = useState(null)
  useEffect(() => { faConsAPI.storeHistory(st_cd).then(({ data }) => setEvents(data.data?.items || [])).catch(() => setEvents([])) }, [st_cd])
  const badge = a => ({ ADD: C.green, REMOVE: C.red }[a] || C.blue)
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.4)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 520, maxWidth: '92vw', maxHeight: '80vh', overflow: 'auto', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>History · <span style={{ fontFamily: 'monospace' }}>{st_cd}</span></div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><X size={18} color={C.textMuted} /></button>
        </div>
        {events === null ? <div style={{ padding: 16, textAlign: 'center', color: C.textMuted, fontSize: 12 }}>Loading…</div>
          : !events.length ? <div style={{ padding: 16, textAlign: 'center', color: C.textMuted, fontSize: 12 }}>No events.</div>
            : <table style={{ width: '100%', fontSize: 12, borderCollapse: 'collapse' }}>
              <thead><tr>{['When', 'Action', 'Status', 'By'].map(h => <th key={h} style={{ textAlign: 'left', padding: '5px 8px', color: C.textSub, fontSize: 11, fontWeight: 700, borderBottom: `1px solid ${C.cardBorder}` }}>{h}</th>)}</tr></thead>
              <tbody>{events.map(e => (
                <tr key={e.id} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
                  <td style={{ padding: '5px 8px', color: C.textSub, whiteSpace: 'nowrap' }}>{fmtDT(e.changed_at)}</td>
                  <td style={{ padding: '5px 8px', fontWeight: 700, color: badge(e.action) }}>{e.action}</td>
                  <td style={{ padding: '5px 8px', color: C.textSub }}>{e.status_at || '—'}{e.in_master === false ? ' (not in master)' : ''}</td>
                  <td style={{ padding: '5px 8px', color: C.textSub }}>{e.changed_by || '—'}</td>
                </tr>))}</tbody>
            </table>}
      </div>
    </div>
  )
}

export default function FaConsStoreListPage() {
  const [mode, setMode] = useState('stores')       // 'stores' | 'validation'
  const [rows, setRows] = useState([])
  const [summary, setSummary] = useState(null)
  const [val, setVal] = useState(null)
  const [loading, setLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState('ALL')
  const [colq, setColq] = useState({})
  const [sort, setSort] = useState({ key: 'priority', dir: 'asc' })
  const [gq, setGq] = useState('')
  const [popFilter, setPopFilter] = useState(null)
  const [histStore, setHistStore] = useState(null)
  const [addOpen, setAddOpen] = useState(false)
  const [addSt, setAddSt] = useState(''); const [addRmk, setAddRmk] = useState('')
  const fileRef = useRef()

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await faConsAPI.storeList()
      setRows(data.data?.items || []); setSummary(data.data?.summary || null)
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed to load store list') }
    finally { setLoading(false) }
  }
  const loadVal = async () => { try { const { data } = await faConsAPI.storeValidation(); setVal(data.data) } catch { /* */ } }
  useEffect(() => { load(); loadVal() }, [])

  const cellVal = (c, r) => String((c.fmt ? c.fmt(r[c.key]) : r[c.key]) ?? '')
  const distinct = useMemo(() => {
    const m = {}
    COLS.forEach(c => { const s = new Set(); rows.forEach(r => s.add(cellVal(c, r))); m[c.key] = [...s].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })) })
    return m
  }, [rows])

  const view = useMemo(() => {
    let out = rows.filter(r => COLS.every(c => passColumn(colq[c.key], cellVal(c, r))))
    if (statusFilter !== 'ALL') out = out.filter(r => r.status === statusFilter)
    if (gq.trim()) out = out.filter(r => matchGlobal(gq, COLS.map(c => cellVal(c, r))))
    if (sort.key) {
      const num = COLS.find(c => c.key === sort.key)?.num
      out = [...out].sort((a, b) => {
        let x = a[sort.key], y = b[sort.key]
        if (num) { x = +x || 0; y = +y || 0; return sort.dir === 'asc' ? x - y : y - x }
        x = String(x ?? ''); y = String(y ?? '')
        return sort.dir === 'asc' ? x.localeCompare(y) : y.localeCompare(x)
      })
    }
    return out
  }, [rows, colq, sort, gq, statusFilter])
  const toggleSort = (key) => setSort(s => s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' })

  const onUpload = async (e) => {
    const file = e.target.files?.[0]; if (!file) return
    const fd = new FormData(); fd.append('file', file)
    try { const { data } = await faConsAPI.storeUpload(fd); toast.success(data.message || 'Uploaded'); load(); loadVal() }
    catch (e) { toast.error(e.response?.data?.detail || 'Upload failed') }
    finally { if (fileRef.current) fileRef.current.value = '' }
  }
  const onAdd = async (st = addSt, rmk = addRmk) => {
    if (!st.trim()) { toast.error('Store code required'); return }
    try { const { data } = await faConsAPI.storeAdd({ st_cd: st.trim(), remarks: rmk || undefined }); toast.success(data.message); setAddOpen(false); setAddSt(''); setAddRmk(''); load(); loadVal() }
    catch (e) { toast.error(e.response?.data?.detail || 'Add failed') }
  }
  const addMissing = async (source) => {
    try { const { data } = await faConsAPI.storeAddMissing(source); toast.success(data.message); load(); loadVal() }
    catch (e) { toast.error(e.response?.data?.detail || 'Bulk add failed') }
  }
  const onDelete = async (r) => {
    if (!window.confirm(`Remove ${r.st_cd} from the FA & CONS store list?`)) return
    try { await faConsAPI.storeRemove(r.id); toast.success('Removed'); load(); loadVal() }
    catch (e) { toast.error(e.response?.data?.detail || 'Delete failed') }
  }

  const th = { padding: '6px 8px', fontSize: 11, fontWeight: 800, color: C.textSub, background: C.headerBg, position: 'sticky', top: 0, userSelect: 'none', borderBottom: `2px solid ${C.cardBorder}` }
  const td = { padding: '4px 8px', color: C.textSub, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', borderTop: `1px solid ${C.cardBorder}`, fontSize: 11 }
  const tiles = [['Stores', summary?.total ?? 0, C.text], ['UPC', summary?.upc ?? 0, C.blue],
    ['Old (opened)', summary?.old ?? 0, C.green], ['Not in master', summary?.not_in_master ?? 0, C.red]]

  return (
    <div className="p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h1 className="page-title">FA &amp; CONS · UPC Store List</h1>
          <p className="text-[11px] text-gray-500 mt-0.5 max-w-3xl">
            The stores FA/CONS allocation runs against. UPC vs Old is read from the store master (opened stores auto-become Old);
            priority = opening date, older first. Upload a list and validate it against the master.
          </p>
        </div>
        <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm shrink-0">
          {[['stores', 'Store List'], ['validation', `Validation${val ? ` (${val.missing_in_master.length + val.missing_in_list.length})` : ''}`]].map(([m, t]) => (
            <button key={m} onClick={() => setMode(m)} className={`px-3 py-1.5 text-[11px] font-semibold ${mode === m ? 'bg-gray-800 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{t}</button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
        <button onClick={() => fileRef.current?.click()} className="btn-primary btn-sm flex items-center gap-1.5 !py-0.5"><Upload size={13} /> Upload list</button>
        <input ref={fileRef} type="file" accept=".xlsx,.xls" onChange={onUpload} className="hidden" />
        <button onClick={() => setAddOpen(true)} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><Plus size={13} /> Add store</button>
        <button onClick={async () => { try { saveBlob(await faConsAPI.storeTemplate(), 'facons_store_list_template.xlsx') } catch { toast.error('Template failed') } }} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><Download size={13} /> Template</button>
        {mode === 'stores' && <button onClick={() => exportCsv('facons_store_list', COLS, view)} disabled={!view.length} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><Download size={13} /> Export</button>}
        <button onClick={() => { load(); loadVal() }} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Refresh</button>
      </div>

      {mode === 'stores' && <>
        <div className="flex flex-wrap gap-2">
          {tiles.map(([k, v, col]) => (
            <div key={k} className="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-1">
              <span className="text-[9px] font-semibold uppercase tracking-wide text-gray-400">{k}</span>
              <span className="text-[13px] font-bold tabular-nums" style={{ color: col }}>{v}</span>
            </div>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-1.5 text-[10px]">
          <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm">
            {STATUS_FILTERS.map(f => (
              <button key={f.key} onClick={() => setStatusFilter(f.key)} className={`px-3 py-0.5 text-[11px] font-semibold ${statusFilter === f.key ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{f.label}</button>
            ))}
          </div>
          <div className="relative">
            <Search size={12} style={{ position: 'absolute', left: 7, top: 6, color: '#9ca3af' }} />
            <input value={gq} onChange={e => setGq(e.target.value)} placeholder="Search all columns (a, b, c = any)…" className="input !py-0.5 !text-[10px]" style={{ paddingLeft: 24, width: 220 }} />
            {gq && <button onClick={() => setGq('')} style={{ position: 'absolute', right: 5, top: 5, background: 'none', border: 'none', cursor: 'pointer' }}><X size={11} color="#9ca3af" /></button>}
          </div>
          {Object.values(colq).some(filterActive) && <button onClick={() => setColq({})} className="btn-secondary btn-sm flex items-center gap-1 !py-0.5"><X size={12} /> Clear</button>}
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
                      <button title="Filter" onClick={e => { const r = e.currentTarget.getBoundingClientRect(); setPopFilter(p => p?.key === c.key ? null : { key: c.key, x: r.left, y: r.bottom }) }} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
                        <Filter size={11} color={active ? C.primary : C.textMuted} fill={active ? C.primary : 'none'} />
                      </button>
                    </div>
                  </th>
                )
              })}
              <th style={{ ...th, textAlign: 'center', width: 66 }}>·</th>
            </tr></thead>
            <tbody>
              {view.map(r => (
                <tr key={r.id} style={{ background: C.card }} onMouseEnter={e => e.currentTarget.style.background = C.rowAlt} onMouseLeave={e => e.currentTarget.style.background = C.card}>
                  {COLS.map(c => {
                    if (c.badge) { const col = statusColor(r.status); return (
                      <td key={c.key} style={td}><span style={{ fontSize: 10, fontWeight: 700, padding: '1px 8px', borderRadius: 999, border: `1px solid ${col.bd}`, background: col.bg, color: col.fg }}>{r.status}</span></td>
                    ) }
                    const raw = c.fmt ? c.fmt(r[c.key]) : (r[c.key] ?? '—')
                    return <td key={c.key} style={{ ...td, textAlign: c.num ? 'right' : 'left', fontFamily: c.mono ? 'monospace' : 'inherit', color: c.num ? C.text : C.textSub, fontWeight: c.num ? 700 : 400 }}>{raw}</td>
                  })}
                  <td style={{ ...td, textAlign: 'center' }}>
                    <button title="History" onClick={() => setHistStore(r.st_cd)} style={{ background: 'none', border: 'none', cursor: 'pointer', marginRight: 8 }}><History size={13} color={C.textMuted} /></button>
                    <button title="Remove" onClick={() => onDelete(r)} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><Trash2 size={13} color={C.textMuted} /></button>
                  </td>
                </tr>
              ))}
              {!view.length && <tr><td colSpan={COLS.length + 1} style={{ padding: 24, textAlign: 'center', color: C.textMuted }}>{loading ? 'Loading…' : 'No stores yet. Upload a list or add a store.'}</td></tr>}
            </tbody>
          </table>
        </div>
      </>}

      {mode === 'validation' && (
        <div className="grid md:grid-cols-2 gap-3">
          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '10px 14px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', alignItems: 'center', gap: 8 }}>
              <AlertTriangle size={15} color={C.red} />
              <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>Not in store master</span>
              <span className="badge" style={{ background: C.redBg, color: C.red, fontWeight: 700 }}>{val?.missing_in_master.length ?? 0}</span>
            </div>
            <div style={{ maxHeight: 'calc(100vh - 330px)', overflow: 'auto' }}>
              {!val?.missing_in_master.length ? <div style={{ padding: 20, textAlign: 'center', color: C.green, fontSize: 12, display: 'flex', gap: 6, justifyContent: 'center', alignItems: 'center' }}><CheckCircle2 size={15} /> Every listed store exists in the master.</div>
                : val.missing_in_master.map(r => (
                  <div key={r.st_cd} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 14px', borderTop: `1px solid ${C.cardBorder}`, fontSize: 12 }}>
                    <span style={{ fontFamily: 'monospace', fontWeight: 600, color: C.text }}>{r.st_cd}</span>
                    <span style={{ color: C.textMuted }}>added by {r.created_by || '—'}</span>
                  </div>
                ))}
            </div>
          </div>

          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '10px 14px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', alignItems: 'center', gap: 8 }}>
              <AlertTriangle size={15} color={C.amber} />
              <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>Have MBQ, missing from list</span>
              <span className="badge" style={{ background: C.amberBg, color: C.amber, fontWeight: 700 }}>{val?.missing_in_list.length ?? 0}</span>
              {!!val?.missing_in_list.length && <button onClick={() => addMissing('mbq')} className="btn-primary btn-sm !py-0.5 !text-[10px] ml-auto flex items-center gap-1"><Plus size={11} /> Add all</button>}
            </div>
            <div style={{ maxHeight: 'calc(100vh - 360px)', overflow: 'auto' }}>
              {!val?.missing_in_list.length ? <div style={{ padding: 20, textAlign: 'center', color: C.green, fontSize: 12, display: 'flex', gap: 6, justifyContent: 'center', alignItems: 'center' }}><CheckCircle2 size={15} /> Every MBQ store is in the list.</div>
                : val.missing_in_list.map(r => (
                  <div key={r.st_cd} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 14px', borderTop: `1px solid ${C.cardBorder}`, fontSize: 12 }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      <span style={{ fontFamily: 'monospace', fontWeight: 600, color: C.text }}>{r.st_cd}</span>
                      <span style={{ color: C.textSub }}> · {r.st_nm || (r.in_master ? '' : 'not in master')}</span>
                      <span style={{ color: C.textMuted }}> · {r.mbq_rows} MBQ</span>
                    </span>
                    <button onClick={() => onAdd(r.st_cd, '')} className="btn-secondary btn-sm !py-0.5 !text-[10px] flex items-center gap-1"><Plus size={11} /> Add</button>
                  </div>
                ))}
            </div>
          </div>

          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '10px 14px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', alignItems: 'center', gap: 8 }}>
              <AlertTriangle size={15} color={C.blue} />
              <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>UPC stores in master, not listed</span>
              <span className="badge" style={{ background: C.blueBg, color: C.blue, fontWeight: 700 }}>{val?.master_upc_not_listed?.length ?? 0}</span>
              {!!val?.master_upc_not_listed?.length && <button onClick={() => addMissing('master_upc')} className="btn-primary btn-sm !py-0.5 !text-[10px] ml-auto flex items-center gap-1"><Plus size={11} /> Add all UPC</button>}
            </div>
            <div style={{ maxHeight: 'calc(100vh - 360px)', overflow: 'auto' }}>
              {!val?.master_upc_not_listed?.length ? <div style={{ padding: 20, textAlign: 'center', color: C.green, fontSize: 12, display: 'flex', gap: 6, justifyContent: 'center', alignItems: 'center' }}><CheckCircle2 size={15} /> All UPC stores from the master are listed.</div>
                : val.master_upc_not_listed.map(r => (
                  <div key={r.st_cd} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 14px', borderTop: `1px solid ${C.cardBorder}`, fontSize: 12 }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      <span style={{ fontFamily: 'monospace', fontWeight: 600, color: C.text }}>{r.st_cd}</span>
                      <span style={{ color: C.textSub }}> · {r.st_nm || ''}</span>
                      <span style={{ color: C.textMuted }}> · opened {fmtDate(r.op_dt)}</span>
                    </span>
                    <button onClick={() => onAdd(r.st_cd, '')} className="btn-secondary btn-sm !py-0.5 !text-[10px] flex items-center gap-1"><Plus size={11} /> Add</button>
                  </div>
                ))}
            </div>
          </div>
        </div>
      )}

      {popFilter && (() => {
        const c = COLS.find(x => x.key === popFilter.key)
        return <ColumnFilterPopover label={c?.label} values={distinct[popFilter.key] || []} value={colq[popFilter.key]} pos={popFilter}
          onChange={f => setColq(q => { const n = { ...q }; if (f) n[popFilter.key] = f; else delete n[popFilter.key]; return n })} onClose={() => setPopFilter(null)} />
      })()}

      {histStore && <HistoryModal st_cd={histStore} onClose={() => setHistStore(null)} />}

      {addOpen && (
        <div onClick={() => setAddOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 380, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: C.text, marginBottom: 12 }}>Add store to list</div>
            <label style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', color: C.textMuted }}>Store code</label>
            <input autoFocus value={addSt} onChange={e => setAddSt(e.target.value)} className="input !py-1 !text-[13px]" style={{ width: '100%', marginTop: 3, marginBottom: 10 }} />
            <label style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', color: C.textMuted }}>Remarks (optional)</label>
            <input value={addRmk} onChange={e => setAddRmk(e.target.value)} className="input !py-1 !text-[13px]" style={{ width: '100%', marginTop: 3 }} />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 14 }}>
              <button onClick={() => setAddOpen(false)} className="btn-secondary btn-sm">Cancel</button>
              <button onClick={() => onAdd()} disabled={!addSt.trim()} className="btn-primary btn-sm">Add</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
