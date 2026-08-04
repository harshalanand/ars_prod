import { useEffect, useMemo, useRef, useState } from 'react'
import { Upload, Download, FileDown, RefreshCw, Trash2, X, ChevronUp, ChevronDown, Filter, Search } from 'lucide-react'
import toast from 'react-hot-toast'
import { faConsAPI } from '@/services/api'
import { C } from '@/theme/colors'
import ColumnFilterPopover, { passColumn, filterActive, matchGlobal } from '@/components/facons/ColumnFilter'

const FILTERS = [
  { key: 'ALL', label: 'All' },
  { key: 'FA', label: 'FA' },
  { key: 'CONS', label: 'CONS' },
]
const TYPE_FILTERS = [
  { key: 'ALL', label: 'All types' },
  { key: 'CENTRAL', label: 'Central' },
  { key: 'LOCAL', label: 'Local' },
]

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function fmtDate(iso) {
  if (!iso) return '—'
  const m = String(iso).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/)
  return m ? `${m[3]}-${MONTHS[+m[2] - 1]}-${m[1]}` : iso
}
function fmtDateTime(iso) { return iso ? `${fmtDate(iso)} ${String(iso).slice(11, 16)}` : '—' }

function saveBlob(res, fallback) {
  const url = window.URL.createObjectURL(new Blob([res.data]))
  const a = document.createElement('a'); a.href = url
  const cd = res.headers?.['content-disposition'] || ''
  const m = cd.match(/filename="?([^"]+)"?/)
  a.download = m ? m[1] : fallback
  document.body.appendChild(a); a.click(); a.remove(); window.URL.revokeObjectURL(url)
}

// Column model — mirrors UPC Store Tracking's table. `w` = default width (px),
// sized to the header/data; every column is drag-resizable at runtime.
const COLS = [
  { key: 'st_cd', label: 'Store', mono: true, w: 78 },
  { key: 'site_name', label: 'Store Name', w: 150 },
  { key: 'rdc', label: 'RDC', w: 66 },
  { key: 'hub', label: 'HUB', w: 66 },
  { key: 'op_dt', label: 'OP Date', fmt: fmtDate, w: 102 },
  { key: 'ref_art', label: 'Ref Article', mono: true, w: 104 },
  { key: 'ref_art_desc', label: 'Ref Art Desc', w: 190 },
  { key: 'div', label: 'DIV', w: 58 },
  { key: 'sub_div', label: 'SUB_DIV', w: 140 },
  { key: 'maj_cat', label: 'MAJ_CAT', w: 176 },
  { key: 'sz', label: 'SZ', w: 56 },
  { key: 'mbq_q', label: 'MBQ_Q', num: true, w: 78 },
  { key: 'stock', label: 'Store Stk', num: true, stock: true, w: 84 },
  { key: 'dc_stock', label: 'DC Pool', num: true, w: 78 },
  { key: 'balance', label: 'Balance', num: true, w: 78 },
  { key: 'fill_rate_pct', label: 'Fill %', num: true, fill: true, w: 70 },
  // Remarks & Approved-By are intentionally NOT table columns — they are tracked
  // as events and shown in the per-row Change History (clock icon).
]

function HistoryModal({ row, onClose }) {
  const [events, setEvents] = useState(null)
  useEffect(() => {
    faConsAPI.mbqHistory(row.st_cd, row.ref_art).then(({ data }) => setEvents(data.data?.items || [])).catch(() => setEvents([]))
  }, [row])
  const th = { textAlign: 'left', padding: '6px 8px', fontSize: 11, fontWeight: 700, color: C.textSub, background: C.headerBg, position: 'sticky', top: 0 }
  const badge = a => ({ CREATE: C.green, DELETE: C.red, APPROVE: C.primary }[a] || C.blue)
  return (
    <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 60, background: 'rgba(0,0,0,.4)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div onClick={e => e.stopPropagation()} style={{ width: 620, maxWidth: '92vw', maxHeight: '80vh', overflow: 'auto', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 18 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
            Change history · <span style={{ fontFamily: 'monospace' }}>{row.st_cd}</span> / <span style={{ fontFamily: 'monospace' }}>{row.ref_art}</span>
          </div>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><X size={18} color={C.textMuted} /></button>
        </div>
        {events === null ? <div style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 12 }}>Loading…</div>
          : !events.length ? <div style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 12 }}>No change events recorded.</div>
            : (
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead><tr>{['When', 'Action', 'MBQ', 'Remark', 'By', 'Source'].map(h => <th key={h} style={th}>{h}</th>)}</tr></thead>
                <tbody>{events.map(e => {
                  const mbqChg = e.old_mbq !== e.new_mbq
                  const rmkChg = (e.old_remarks || '') !== (e.new_remarks || '')
                  return (
                    <tr key={e.id} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
                      <td style={{ padding: '5px 8px', color: C.textSub, whiteSpace: 'nowrap' }}>{fmtDateTime(e.changed_at)}</td>
                      <td style={{ padding: '5px 8px' }}><span style={{ fontWeight: 700, color: badge(e.action) }}>{e.action}</span></td>
                      <td style={{ padding: '5px 8px', whiteSpace: 'nowrap', color: C.text }}>
                        {mbqChg ? <><span style={{ color: C.textMuted }}>{e.old_mbq ?? '—'}</span> → <b>{e.new_mbq ?? '—'}</b></> : (e.new_mbq ?? '—')}
                      </td>
                      <td style={{ padding: '5px 8px', color: C.textSub }} title={rmkChg ? `${e.old_remarks || '(blank)'} → ${e.new_remarks || '(blank)'}` : (e.new_remarks || '')}>
                        {rmkChg ? <span style={{ color: C.amber, fontWeight: 600 }}>{e.new_remarks || '(cleared)'}</span> : (e.new_remarks || '—')}
                      </td>
                      <td style={{ padding: '5px 8px', color: C.textSub, fontWeight: 600 }}>{e.changed_by || '—'}</td>
                      <td style={{ padding: '5px 8px', color: C.textMuted }}>{e.source || '—'}</td>
                    </tr>)
                })}</tbody>
              </table>
            )}
      </div>
    </div>
  )
}

// ── Change Review ───────────────────────────────────────────────────────────
// On-demand audit of MBQ changes for a single date or a date range. Reads
// ARS_FACONS_MBQ_HIST (read-only) via /fa-cons/mbq/change-review.
const RCOLS = [
  { key: 'changed_at', label: 'When', fmt: fmtDateTime, w: 140 },
  { key: 'st_cd', label: 'Store', mono: true, w: 78 },
  { key: 'ref_art', label: 'Ref Article', mono: true, w: 104 },
  { key: 'stream', label: 'Stream', w: 64 },
  { key: 'action', label: 'Action', w: 82 },
  { key: 'mbq', label: 'MBQ (old → new)', num: true, w: 130 },
  { key: 'remarks', label: 'Remarks (old → new)', w: 210 },
  { key: 'reason', label: 'Reason', w: 130 },
  { key: 'changed_by', label: 'By', w: 100 },
  { key: 'source', label: 'Source', w: 84 },
  { key: 'session_id', label: 'Session', w: 90 },
]

// LOCAL calendar date (not toISOString, which is UTC and can be a day behind for
// timezones ahead of UTC — that would drop "today"s events from the default range).
function isoDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
function daysAgo(n) { const d = new Date(); d.setDate(d.getDate() - n); return isoDate(d) }

function ChangeReviewPanel() {
  const [dateMode, setDateMode] = useState('range')     // 'date' | 'range'
  const [from, setFrom] = useState(daysAgo(7))
  const [to, setTo] = useState(daysAgo(0))
  const [onDate, setOnDate] = useState(daysAgo(0))
  const [stream, setStream] = useState('')
  const [sessionId, setSessionId] = useState('')       // selected upload batch (session-wise review)
  const [sessions, setSessions] = useState([])
  const [cardFilter, setCardFilter] = useState(null)   // client-side action filter set by clicking a summary card
  const [gq, setGq] = useState('')
  const [sort, setSort] = useState({ key: 'changed_at', dir: 'desc' })
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [ran, setRan] = useState(false)

  // server query params. A selected `session_id` scopes to one upload batch (and the
  // backend ignores the date window). `action` is applied CLIENT-side via cardFilter so
  // the summary cards always show full counts; export mirrors the visible card filter.
  const baseParams = (over = {}) => {
    const sid = over.session_id !== undefined ? over.session_id : sessionId
    const strm = over.stream !== undefined ? over.stream : stream
    const p = {}
    if (strm) p.stream = strm
    if (sid) { p.session_id = sid; return p }
    const df = dateMode === 'date' ? onDate : from
    const dt = dateMode === 'date' ? onDate : to
    if (df) p.date_from = df
    if (dt) p.date_to = dt
    return p
  }
  const run = async (over = {}) => {
    setLoading(true)
    try {
      const { data: res } = await faConsAPI.mbqChangeReview(baseParams(over))
      setData(res.data); setRan(true)
    } catch (e) { toast.error(e.response?.data?.detail || 'Change review failed') }
    finally { setLoading(false) }
  }
  const loadSessions = async () => {
    try { const { data: res } = await faConsAPI.mbqSessions({ limit: 50 }); setSessions(res.data?.items || []) }
    catch { /* non-fatal */ }
  }
  const onExport = async () => {
    try { saveBlob(await faConsAPI.mbqChangeReviewExport({ ...baseParams(), ...(cardFilter ? { action: cardFilter } : {}) }), 'mbq_change_review.xlsx') }
    catch { toast.error('Export failed') }
  }
  useEffect(() => { run(); loadSessions() /* initial */ /* eslint-disable-next-line */ }, [])

  const selectSession = (v) => { setSessionId(v); setCardFilter(null); setGq(''); run({ session_id: v }) }
  // Refresh = reload latest data AND show ALL changes (drop session + stream + card + search)
  const refresh = () => { setSessionId(''); setCardFilter(null); setGq(''); setStream(''); loadSessions(); run({ stream: '', session_id: '' }) }
  // Clear filters = drop the active view filters instantly
  const clearFilters = () => { setSessionId(''); setCardFilter(null); setGq(''); setStream(''); run({ stream: '', session_id: '' }) }
  const anyFilter = !!cardFilter || !!gq.trim() || !!stream || !!sessionId

  const sessLabel = (s) => {
    const dt = fmtDateTime(s.created_at)
    const chg = (s.n_created || 0) + (s.n_updated || 0)
    const who = s.created_by ? ` · ${s.created_by}` : ''
    const why = s.reason ? ` · ${s.reason.length > 28 ? s.reason.slice(0, 28) + '…' : s.reason}` : ''
    return `#${s.id} · ${dt}${who}${why} (${chg}Δ)`
  }

  const applyPreset = (kind) => {
    if (kind === 'today') { setDateMode('date'); setOnDate(daysAgo(0)) }
    else if (kind === 'yday') { setDateMode('date'); setOnDate(daysAgo(1)) }
    else if (kind === '7') { setDateMode('range'); setFrom(daysAgo(7)); setTo(daysAgo(0)) }
    else if (kind === '30') { setDateMode('range'); setFrom(daysAgo(30)); setTo(daysAgo(0)) }
  }

  const items = data?.items || []
  const view = useMemo(() => {
    let out = items
    if (cardFilter) out = out.filter(r => r.action === cardFilter)
    if (gq.trim()) out = out.filter(r => matchGlobal(gq, [r.st_cd, r.ref_art, r.stream, r.action, r.reason,
      r.changed_by, r.source, r.old_remarks, r.new_remarks, r.old_mbq, r.new_mbq]))
    if (sort.key) {
      out = [...out].sort((a, b) => {
        let x, y
        if (sort.key === 'mbq') { x = +a.new_mbq || 0; y = +b.new_mbq || 0; return sort.dir === 'asc' ? x - y : y - x }
        if (sort.key === 'remarks') { x = a.new_remarks || ''; y = b.new_remarks || '' }
        else { x = a[sort.key]; y = b[sort.key] }
        x = String(x ?? ''); y = String(y ?? '')
        return sort.dir === 'asc' ? x.localeCompare(y) : y.localeCompare(x)
      })
    }
    return out
  }, [items, cardFilter, gq, sort])
  const toggleSort = (key) => setSort(s => s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'desc' })
  // click a card: the 4 action cards toggle a client-side action filter; the others
  // (Total / Stores / Ref articles / Net Δ) clear it → "show all".
  const onCard = (act) => setCardFilter(cur => (act && cur === act) ? null : (act || null))

  const s = data?.summary || {}
  // [label, value, color, actionKey|null] — actionKey drives the client-side filter
  const tiles = [['Total events', s.total ?? 0, C.text, null], ['Created', s.created ?? 0, C.green, 'CREATE'],
    ['Changed', s.updated ?? 0, C.amber, 'UPDATE'], ['Deleted', s.deleted ?? 0, C.red, 'DELETE'],
    ...(s.approved ? [['Approved', s.approved, C.primary, 'APPROVE']] : []),
    ['Stores', s.stores ?? 0, C.blue, null], ['Ref articles', s.ref_arts ?? 0, C.blue, null],
    ['Net MBQ Δ', (s.net_delta ?? 0) > 0 ? `+${s.net_delta}` : (s.net_delta ?? 0), (s.net_delta ?? 0) >= 0 ? C.green : C.red, null]]

  const th = { padding: '6px 8px', fontSize: 11, fontWeight: 800, color: C.textSub, background: C.headerBg, position: 'sticky', top: 0, userSelect: 'none', borderBottom: `2px solid ${C.cardBorder}` }
  const td = { padding: '5px 8px', color: C.textSub, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', borderTop: `1px solid ${C.cardBorder}` }
  const badge = a => ({ CREATE: C.green, UPDATE: C.amber, DELETE: C.red }[a] || C.textSub)
  const lbl = { fontSize: 10, fontWeight: 700, color: C.textMuted, textTransform: 'uppercase', letterSpacing: '.03em' }

  return (
    <>
      <div className="card" style={{ padding: 12 }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end', gap: 12 }}>
          <div>
            <div style={lbl}>Mode</div>
            <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden mt-1">
              {[['date', 'On date'], ['range', 'Date range']].map(([m, t]) => (
                <button key={m} onClick={() => setDateMode(m)}
                  className={`px-3 py-1 text-[11px] font-semibold ${dateMode === m ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{t}</button>
              ))}
            </div>
          </div>
          {dateMode === 'date' ? (
            <div><div style={lbl}>Date</div><input type="date" value={onDate} onChange={e => setOnDate(e.target.value)} className="input !py-1 !text-[11px] mt-1" /></div>
          ) : (
            <>
              <div><div style={lbl}>From</div><input type="date" value={from} onChange={e => setFrom(e.target.value)} className="input !py-1 !text-[11px] mt-1" /></div>
              <div><div style={lbl}>To</div><input type="date" value={to} onChange={e => setTo(e.target.value)} className="input !py-1 !text-[11px] mt-1" /></div>
            </>
          )}
          <div>
            <div style={lbl}>Stream</div>
            <select value={stream} onChange={e => setStream(e.target.value)} className="input !py-1 !text-[11px] mt-1">
              <option value="">All</option><option value="FA">FA</option><option value="CONS">CONS</option>
            </select>
          </div>
          <div>
            <div style={lbl}>Upload session</div>
            <select value={sessionId} onChange={e => selectSession(e.target.value)} className="input !py-1 !text-[11px] mt-1" style={{ maxWidth: 300 }} title="Review the changes made by one upload batch">
              <option value="">All uploads (by date)</option>
              {sessions.map(s => <option key={s.id} value={s.id}>{sessLabel(s)}</option>)}
            </select>
          </div>
          <button onClick={() => run()} className="btn-primary btn-sm flex items-center gap-1.5"><Search size={14} /> Run review</button>
          <button onClick={refresh} title="Reload latest data and show all changes" className="btn-secondary btn-sm flex items-center gap-1.5"><RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh</button>
          <button onClick={onExport} disabled={!view.length} className="btn-secondary btn-sm flex items-center gap-1.5"><FileDown size={14} /> Export</button>
          <div className="flex items-center gap-1 ml-auto">
            {[['today', 'Today'], ['yday', 'Yesterday'], ['7', '7 days'], ['30', '30 days']].map(([k, t]) => (
              <button key={k} onClick={() => applyPreset(k)} className="btn-secondary btn-sm !py-1 !px-2 !text-[10px]">{t}</button>
            ))}
          </div>
        </div>
      </div>

      <div className="flex flex-nowrap gap-2 overflow-x-auto">
        {tiles.map(([k, v, col, act]) => {
          const active = act && cardFilter === act
          return (
            <button key={k} onClick={() => onCard(act)} type="button"
              title={act ? `Show only ${k.toLowerCase()}${active ? ' (click to clear)' : ''}` : 'Show all changes'}
              className="stat-card text-left transition-all"
              style={{ flex: '1 1 0', minWidth: 96, padding: '8px 10px', cursor: 'pointer', outline: active ? `2px solid ${col}` : 'none', outlineOffset: -1, boxShadow: active ? `0 0 0 3px ${col}22` : undefined }}>
              <div className="text-[10px] uppercase tracking-wide text-gray-400 truncate">{k}{active ? ' ▾' : ''}</div>
              <div className="stat-value" style={{ color: col, fontSize: 20 }}>{v}</div>
            </button>
          )
        })}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search size={13} style={{ position: 'absolute', left: 8, top: 7, color: '#9ca3af' }} />
          <input value={gq} onChange={e => setGq(e.target.value)} placeholder="Search changes…" className="input !py-1 !text-[11px]" style={{ paddingLeft: 26, width: 220 }} />
          {gq && <button onClick={() => setGq('')} style={{ position: 'absolute', right: 6, top: 6, background: 'none', border: 'none', cursor: 'pointer' }}><X size={12} color="#9ca3af" /></button>}
        </div>
        {cardFilter && (
          <span className="inline-flex items-center gap-1 text-[11px] font-semibold" style={{ padding: '2px 8px', borderRadius: 999, background: C.primaryLt, color: C.primary }}>
            {{ CREATE: 'Created', UPDATE: 'Changed', DELETE: 'Deleted', APPROVE: 'Approved' }[cardFilter] || cardFilter}
            <button onClick={() => setCardFilter(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', display: 'inline-flex' }}><X size={11} color={C.primary} /></button>
          </span>
        )}
        {anyFilter && (
          <button onClick={clearFilters} className="btn-secondary btn-sm flex items-center gap-1.5"><X size={13} /> Clear filters</button>
        )}
        <span className="text-[11px] text-gray-400">{view.length} of {items.length} events{s.capped ? ` (capped at ${s.shown})` : ''}</span>
      </div>

      <div style={{ overflow: 'auto', maxHeight: 'calc(100vh - 380px)', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 10 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead><tr>{RCOLS.map(c => (
            <th key={c.key} style={{ ...th, textAlign: c.num ? 'right' : 'left', cursor: 'pointer' }} onClick={() => toggleSort(c.key)}>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>{c.label}{sort.key === c.key && (sort.dir === 'asc' ? <ChevronUp size={11} /> : <ChevronDown size={11} />)}</span>
            </th>
          ))}</tr></thead>
          <tbody>
            {view.map(r => {
              const mbqChg = r.action === 'UPDATE' && r.old_mbq !== r.new_mbq
              const rmkChg = (r.old_remarks || '') !== (r.new_remarks || '')
              return (
                <tr key={r.id} style={{ background: C.card }} onMouseEnter={e => e.currentTarget.style.background = C.rowAlt} onMouseLeave={e => e.currentTarget.style.background = C.card}>
                  <td style={{ ...td, color: C.textSub }}>{fmtDateTime(r.changed_at)}</td>
                  <td style={{ ...td, fontFamily: 'monospace' }}>{r.st_cd}</td>
                  <td style={{ ...td, fontFamily: 'monospace' }}>{r.ref_art}</td>
                  <td style={td}>{r.stream}</td>
                  <td style={{ ...td, fontWeight: 700, color: badge(r.action) }}>{r.action}</td>
                  <td style={{ ...td, textAlign: 'right', color: C.text }}>
                    {mbqChg ? <><span style={{ color: C.textMuted }}>{r.old_mbq ?? '—'}</span> → <b>{r.new_mbq ?? '—'}</b></>
                      : r.action === 'DELETE' ? <span style={{ color: C.textMuted }}>{r.old_mbq ?? '—'} → —</span>
                        : <b>{r.new_mbq ?? '—'}</b>}
                  </td>
                  <td style={td} title={rmkChg ? `${r.old_remarks || '(blank)'} → ${r.new_remarks || '(blank)'}` : (r.new_remarks || '')}>
                    {rmkChg ? <span style={{ color: C.amber, fontWeight: 600 }}>{r.new_remarks || '(cleared)'}</span> : (r.new_remarks || '—')}
                  </td>
                  <td style={td}>{r.reason || '—'}</td>
                  <td style={{ ...td, fontWeight: 600 }}>{r.changed_by || '—'}</td>
                  <td style={{ ...td, color: C.textMuted }}>{r.source || '—'}</td>
                  <td style={td}>
                    {r.session_id
                      ? <button onClick={() => selectSession(String(r.session_id))} title="Review only this upload session"
                          style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.primary, fontWeight: 700, fontFamily: 'monospace', padding: 0 }}>#{r.session_id}</button>
                      : '—'}
                  </td>
                </tr>
              )
            })}
            {!view.length && (
              <tr><td colSpan={RCOLS.length} style={{ padding: 24, textAlign: 'center', color: C.textMuted }}>
                {loading ? 'Loading…' : ran ? 'No MBQ changes in this period.' : 'Choose a date or range and Run review.'}
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}

export default function FaConsMbqMasterPage() {
  const [mode, setMode] = useState('mbq')              // 'mbq' | 'review'
  const [filter, setFilter] = useState('ALL')
  const [typeFilter, setTypeFilter] = useState('ALL')   // source_type: ALL | CENTRAL | LOCAL
  const [rows, setRows] = useState([])
  const [summary, setSummary] = useState(null)
  const [loading, setLoading] = useState(false)
  const [colq, setColq] = useState({})                 // per-column text filter
  const [sort, setSort] = useState({ key: null, dir: 'asc' })
  const [hover, setHover] = useState(null)             // { r, x, y } stock breakup
  const [histRow, setHistRow] = useState(null)
  const [colW, setColW] = useState(() => Object.fromEntries(COLS.map(c => [c.key, c.w])))
  const [popFilter, setPopFilter] = useState(null)     // { key, x, y } open filter popover
  const [preview, setPreview] = useState(null)         // { file, data, message } upload confirm
  const [uploadReason, setUploadReason] = useState('') // batch change reason (stamped on events)
  const [committing, setCommitting] = useState(false)
  const [delRow, setDelRow] = useState(null)           // MBQ row pending delete (needs a reason)
  const [delReason, setDelReason] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [gq, setGq] = useState('')                     // global search across all columns
  const [histHover, setHistHover] = useState(null)     // { row, x, y, events } — change-history preview on hover
  const fileRef = useRef()
  const drag = useRef(null)
  const histCache = useRef({})                          // { 'st_cd|ref_art': events[] }

  const onHistEnter = (r, e) => {
    const key = `${r.st_cd}|${r.ref_art}`
    setHistHover({ row: r, x: e.clientX, y: e.clientY, events: histCache.current[key] || null })
    if (!histCache.current[key]) {
      faConsAPI.mbqHistory(r.st_cd, r.ref_art)
        .then(({ data }) => {
          histCache.current[key] = data.data?.items || []
          setHistHover(h => (h && h.row.id === r.id) ? { ...h, events: histCache.current[key] } : h)
        }).catch(() => {})
    }
  }
  const hbadge = a => ({ CREATE: C.green, UPDATE: C.amber, DELETE: C.red, APPROVE: C.primary }[a] || C.blue)

  // drag-to-resize a column
  const startResize = (key, e) => {
    e.preventDefault(); e.stopPropagation()
    drag.current = { key, startX: e.clientX, startW: colW[key] || 100 }
    const move = ev => setColW(w => ({ ...w, [key]: Math.max(44, drag.current.startW + (ev.clientX - drag.current.startX)) }))
    const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up) }
    document.addEventListener('mousemove', move); document.addEventListener('mouseup', up)
  }

  const load = async (f = filter) => {
    setLoading(true)
    try {
      const { data } = await faConsAPI.listMbq(f === 'ALL' ? undefined : f)
      setRows(data.data?.items || []); setSummary(data.data?.summary || null)
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed to load MBQ master') }
    finally { setLoading(false) }
  }
  useEffect(() => { load(filter) /* eslint-disable-next-line */ }, [filter])

  // Step 1: dry-run preview — NO write. Opens a confirm dialog.
  const onUpload = async (e) => {
    const file = e.target.files?.[0]; if (!file) return
    const fd = new FormData(); fd.append('file', file)
    try {
      const { data } = await faConsAPI.uploadMbq(fd, { dryRun: true })
      setUploadReason('')
      setPreview({ file, data: data.data, message: data.message })
    } catch (e) { toast.error(e.response?.data?.detail || 'Upload preview failed') }
    finally { if (fileRef.current) fileRef.current.value = '' }
  }
  // Step 2: commit the reviewed file (dry_run=false).
  const onConfirmUpload = async () => {
    if (!preview) return
    if (!uploadReason.trim()) { toast.error('Reason for this change is mandatory'); return }
    setCommitting(true)
    const fd = new FormData(); fd.append('file', preview.file)
    try {
      const { data } = await faConsAPI.uploadMbq(fd, { dryRun: false, reason: uploadReason.trim() || undefined })
      toast[data.success ? 'success' : 'error'](data.message || 'Uploaded')
      if (data.data?.errors?.length) data.data.errors.slice(0, 5).forEach(x => toast.error(x))
      setPreview(null); load(filter)
    } catch (e) { toast.error(e.response?.data?.detail || 'Upload failed') }
    finally { setCommitting(false) }
  }
  const onTemplate = async () => { try { saveBlob(await faConsAPI.templateMbq(), 'facons_mbq_template.xlsx') } catch { toast.error('Template failed') } }
  const onExport = async () => { try { saveBlob(await faConsAPI.exportMbq(filter === 'ALL' ? undefined : filter), 'facons_mbq.xlsx') } catch { toast.error('Export failed') } }
  const onDelete = (r) => { setDelReason(''); setDelRow(r) }   // open confirm modal (reason mandatory)
  const onConfirmDelete = async () => {
    if (!delRow) return
    if (!delReason.trim()) { toast.error('Reason for this deletion is mandatory'); return }
    setDeleting(true)
    try {
      await faConsAPI.removeMbq(delRow.id, delReason.trim())
      toast.success('Removed'); setDelRow(null); load(filter)
    } catch (e) { toast.error(e.response?.data?.detail || 'Delete failed') }
    finally { setDeleting(false) }
  }
  const toggleSort = (key) => setSort(s => s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' })

  // distinct formatted values per column — feeds the faceted header filter
  const cellVal = (c, r) => String((c.fmt ? c.fmt(r[c.key]) : r[c.key]) ?? '')
  const distinct = useMemo(() => {
    const m = {}
    COLS.forEach(c => {
      const set = new Set()
      rows.forEach(r => set.add(cellVal(c, r)))
      m[c.key] = [...set].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    })
    return m
  }, [rows])

  const view = useMemo(() => {
    // per-column faceted filter: ticked distinct values (exact) OR comma-separated text
    // (contains), include/exclude; columns combine with AND. Global search = comma OR.
    let out = rows.filter(r => COLS.every(c => passColumn(colq[c.key], cellVal(c, r))))
    if (typeFilter !== 'ALL') out = out.filter(r => (r.source_type || 'CENTRAL') === typeFilter)
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
  }, [rows, colq, sort, gq, typeFilter])

  const bs = summary?.by_stream || {}
  const tiles = [['Rows', summary?.rows ?? 0], ['Fixed Assets (FA)', bs.FA ?? 0],
    ['Consumables (CONS)', bs.CONS ?? 0], ['Total MBQ', Math.round(summary?.total_mbq ?? 0).toLocaleString()]]

  const fillColor = v => v == null ? C.textMuted : v >= 100 ? C.green : v >= 60 ? C.amber : C.red
  const th = { padding: '6px 8px', fontSize: 11, fontWeight: 800, color: C.textSub, background: C.headerBg, position: 'sticky', top: 0, userSelect: 'none', borderBottom: `2px solid ${C.cardBorder}` }
  const td = { padding: '5px 8px', color: C.textSub, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', borderTop: `1px solid ${C.cardBorder}` }
  const nFilters = Object.values(colq).filter(f => f?.q?.trim()).length

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h1 className="page-title">FA &amp; CONS · MBQ Master</h1>
          <p className="text-[11px] text-gray-500 mt-0.5">
            {mode === 'mbq'
              ? <>Upload one sheet (<b>ST_CD · REF_ART · MBQ_Q</b>) — upsert; rows auto-segregate FA (DIV=FA) / CONS (DIV=CO). Stock &amp; fill-rate use the active store SLOCs — hover the Stock cell for the SLOC breakup. Click a header to sort; filter any column.</>
              : <>On-demand audit of MBQ changes for a single date or a date range — created, changed and deleted MBQ rows with old → new values.</>}
          </p>
        </div>
        {/* tab toggle — pinned top-right, identical position in both modes */}
        <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm shrink-0">
          {[['mbq', 'MBQ Master'], ['review', 'Change Review']].map(([m, t]) => (
            <button key={m} onClick={() => setMode(m)}
              className={`px-3 py-1.5 text-[11px] font-semibold transition-colors ${mode === m ? 'bg-gray-800 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              {t}
            </button>
          ))}
        </div>
      </div>

      {mode === 'review' && <ChangeReviewPanel />}

      {mode === 'mbq' && <>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {tiles.map(([k, v]) => (
          <div key={k} className="stat-card"><div className="text-[10px] uppercase tracking-wide text-gray-400">{k}</div><div className="stat-value">{v}</div></div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm mr-1">
          {FILTERS.map(f => (
            <button key={f.key} onClick={() => setFilter(f.key)}
              className={`px-3 py-1.5 text-[11px] font-semibold transition-colors ${filter === f.key ? 'bg-primary-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              {f.label}
            </button>
          ))}
        </div>
        <div className="inline-flex rounded-lg border border-gray-300 overflow-hidden shadow-sm mr-1" title="Filter by sourcing type">
          {TYPE_FILTERS.map(f => (
            <button key={f.key} onClick={() => setTypeFilter(f.key)}
              className={`px-3 py-1.5 text-[11px] font-semibold transition-colors ${typeFilter === f.key ? 'bg-gray-800 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              {f.label}
            </button>
          ))}
        </div>
        <button onClick={() => fileRef.current?.click()} className="btn-primary btn-sm flex items-center gap-1.5"><Upload size={14} /> Upload MBQ</button>
        <input ref={fileRef} type="file" accept=".xlsx,.xls" onChange={onUpload} className="hidden" />
        <button onClick={onTemplate} className="btn-secondary btn-sm flex items-center gap-1.5"><Download size={14} /> Template</button>
        <button onClick={onExport} className="btn-secondary btn-sm flex items-center gap-1.5"><FileDown size={14} /> Export</button>
        <button onClick={() => load(filter)} className="btn-secondary btn-sm flex items-center gap-1.5"><RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> Refresh</button>
        {Object.values(colq).some(filterActive) && (
          <button onClick={() => setColq({})} className="btn-secondary btn-sm flex items-center gap-1.5"><X size={13} /> Clear filters</button>
        )}
        <div className="relative ml-auto">
          <Search size={13} style={{ position: 'absolute', left: 8, top: 7, color: '#9ca3af' }} />
          <input value={gq} onChange={e => setGq(e.target.value)} placeholder="Search all columns (a, b, c = any)…"
            className="input !py-1 !text-[11px]" style={{ paddingLeft: 26, width: 240 }} />
          {gq && <button onClick={() => setGq('')} style={{ position: 'absolute', right: 6, top: 6, background: 'none', border: 'none', cursor: 'pointer' }}><X size={12} color="#9ca3af" /></button>}
        </div>
        <span className="text-[11px] text-gray-400">
          {view.length} of {rows.length} rows
        </span>
      </div>

      <div style={{ overflow: 'auto', maxHeight: 'calc(100vh - 340px)', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 10 }}>
        <table style={{ tableLayout: 'fixed', borderCollapse: 'collapse', fontSize: 12 }}>
          <colgroup>
            {COLS.map(c => <col key={c.key} style={{ width: colW[c.key] }} />)}
            <col style={{ width: 68 }} />
          </colgroup>
          <thead>
            <tr>
              {COLS.map(c => {
                const active = filterActive(colq[c.key])
                return (
                  <th key={c.key} style={{ ...th, position: 'sticky', top: 0, textAlign: c.num ? 'right' : 'left' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 3, justifyContent: c.num ? 'flex-end' : 'space-between' }}>
                      <span onClick={() => toggleSort(c.key)} title="Sort" style={{ display: 'inline-flex', alignItems: 'center', gap: 1, cursor: 'pointer', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {c.label}
                        {sort.key === c.key && (sort.dir === 'asc' ? <ChevronUp size={11} /> : <ChevronDown size={11} />)}
                      </span>
                      <button title="Filter" onClick={e => { const r = e.currentTarget.getBoundingClientRect(); setPopFilter(p => p?.key === c.key ? null : { key: c.key, x: r.left, y: r.bottom }) }}
                        style={{ flexShrink: 0, display: 'inline-flex', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
                        <Filter size={11} color={active ? C.primary : C.textMuted} fill={active ? C.primary : 'none'} />
                      </button>
                    </div>
                    {/* drag-to-resize handle */}
                    <div onMouseDown={e => startResize(c.key, e)} style={{ position: 'absolute', top: 0, right: 0, width: 6, height: '100%', cursor: 'col-resize' }} />
                  </th>
                )
              })}
              <th style={{ ...th, position: 'sticky', top: 0, textAlign: 'center' }}>·</th>
            </tr>
          </thead>
          <tbody>
            {view.map(r => (
              <tr key={r.id} style={{ background: C.card }}
                onMouseEnter={e => e.currentTarget.style.background = C.rowAlt}
                onMouseLeave={e => e.currentTarget.style.background = C.card}>
                {COLS.map(c => {
                  const raw = c.fmt ? c.fmt(r[c.key]) : (r[c.key] ?? '—')
                  if (c.stock) return (
                    <td key={c.key} style={{ ...td, textAlign: 'right', fontWeight: 700, color: r.stock ? C.text : C.textMuted, cursor: 'help', textDecoration: r.stock ? 'underline dotted' : 'none' }}
                      onMouseEnter={e => setHover({ r, x: e.clientX, y: e.clientY })}
                      onMouseMove={e => setHover(h => h ? { ...h, x: e.clientX, y: e.clientY } : h)}
                      onMouseLeave={() => setHover(null)}>{r.stock ?? 0}</td>
                  )
                  if (c.fill) return (
                    <td key={c.key} style={{ ...td, textAlign: 'right', fontWeight: 700, color: fillColor(r.fill_rate_pct) }}>{r.fill_rate_pct == null ? '—' : `${r.fill_rate_pct}%`}</td>
                  )
                  return <td key={c.key} style={{ ...td, textAlign: c.num ? 'right' : 'left', fontFamily: c.mono ? 'monospace' : 'inherit', color: c.num ? C.text : C.textSub, fontWeight: c.num ? 600 : 400 }}>{raw}</td>
                })}
                <td style={{ ...td, textAlign: 'center' }}>
                  {r.hist_count > 0 && (
                    <button title="Change history — hover to preview, click to open"
                      onClick={() => setHistRow(r)}
                      onMouseEnter={e => onHistEnter(r, e)}
                      onMouseMove={e => setHistHover(h => h ? { ...h, x: e.clientX, y: e.clientY } : h)}
                      onMouseLeave={() => setHistHover(null)}
                      style={{ background: C.primaryLt, color: C.primary, border: 'none', borderRadius: 999, minWidth: 22, height: 18, fontSize: 11, fontWeight: 700, cursor: 'pointer', marginRight: 8, padding: '0 6px', lineHeight: '18px' }}>
                      {r.hist_count}
                    </button>
                  )}
                  <button title="Delete" onClick={() => onDelete(r)} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><Trash2 size={13} color={C.textMuted} /></button>
                </td>
              </tr>
            ))}
            {!view.length && (
              <tr><td colSpan={COLS.length + 1} style={{ padding: 24, textAlign: 'center', color: C.textMuted }}>{loading ? 'Loading…' : 'No MBQ rows. Upload a sheet with ST_CD · REF_ART · MBQ_Q.'}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {/* floating stock-breakup hover card (like UPC store tracking) */}
      {hover && (
        <div style={{ position: 'fixed', left: Math.min(hover.x + 14, window.innerWidth - 220), top: Math.min(hover.y + 14, window.innerHeight - 200), zIndex: 70, width: 200, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,.12)', padding: 10, pointerEvents: 'none' }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textMuted, marginBottom: 4 }}>
            SLOC-wise stock · <span style={{ fontFamily: 'monospace' }}>{hover.r.st_cd}</span> / <span style={{ fontFamily: 'monospace' }}>{hover.r.ref_art}</span>
          </div>
          {Object.entries(hover.r.sloc || {}).length ? Object.entries(hover.r.sloc).map(([sl, q]) => (
            <div key={sl} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }}>
              <span style={{ fontFamily: 'monospace', color: C.textSub }}>{sl}</span><span style={{ color: C.text, fontWeight: 600 }}>{q}</span>
            </div>
          )) : <div style={{ fontSize: 11, color: C.textMuted }}>No stock in active SLOCs</div>}
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, borderTop: `1px solid ${C.cardBorder}`, marginTop: 4, paddingTop: 4, fontWeight: 700 }}>
            <span>Total</span><span>{hover.r.stock ?? 0}</span>
          </div>
          {hover.r.fill_rate_pct != null && (
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, fontWeight: 700, color: fillColor(hover.r.fill_rate_pct) }}>
              <span>Fill %</span><span>{hover.r.fill_rate_pct}%</span>
            </div>
          )}
        </div>
      )}

      {/* floating change-history preview on hover of the count badge */}
      {histHover && (
        <div style={{ position: 'fixed', left: Math.min(histHover.x + 14, window.innerWidth - 330), top: Math.min(histHover.y + 14, window.innerHeight - 250), zIndex: 70, width: 314, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,.14)', padding: 10, pointerEvents: 'none' }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textMuted, marginBottom: 4 }}>
            Change history · <span style={{ fontFamily: 'monospace' }}>{histHover.row.st_cd}</span> / <span style={{ fontFamily: 'monospace' }}>{histHover.row.ref_art}</span>
          </div>
          {histHover.events === null ? <div style={{ fontSize: 11, color: C.textMuted }}>Loading…</div>
            : !histHover.events.length ? <div style={{ fontSize: 11, color: C.textMuted }}>No change events.</div>
              : histHover.events.slice(0, 6).map(e => {
                const mbqChg = e.old_mbq !== e.new_mbq
                return (
                  <div key={e.id} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 11, borderTop: `1px solid ${C.cardBorder}`, padding: '3px 0' }}>
                    <span style={{ color: C.textMuted, whiteSpace: 'nowrap' }}>{fmtDateTime(e.changed_at)}</span>
                    <span style={{ fontWeight: 700, color: hbadge(e.action) }}>{e.action}</span>
                    <span style={{ color: C.text, whiteSpace: 'nowrap' }}>{mbqChg ? `${e.old_mbq ?? '—'} → ${e.new_mbq ?? '—'}` : (e.new_mbq ?? '—')}</span>
                  </div>
                )
              })}
          {histHover.events && histHover.events.length > 6 && (
            <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4 }}>+{histHover.events.length - 6} more — click to open</div>
          )}
        </div>
      )}

      {/* per-column faceted filter popover — distinct-value checklist + search + multi-select */}
      {popFilter && (() => {
        const c = COLS.find(x => x.key === popFilter.key)
        return (
          <ColumnFilterPopover label={c?.label} values={distinct[popFilter.key] || []} value={colq[popFilter.key]} pos={popFilter}
            onChange={f => setColq(q => { const n = { ...q }; if (f) n[popFilter.key] = f; else delete n[popFilter.key]; return n })}
            onClose={() => setPopFilter(null)} />
        )
      })()}

      {histRow && <HistoryModal row={histRow} onClose={() => setHistRow(null)} />}

      {/* Delete confirmation — reason is mandatory (recorded on the DELETE event) */}
      {delRow && (
        <div onClick={() => !deleting && setDelRow(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 440, maxWidth: '92vw', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
            <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: C.text }}>Delete MBQ</div>
              <button onClick={() => !deleting && setDelRow(null)} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><X size={18} color={C.textMuted} /></button>
            </div>
            <div style={{ padding: 16 }}>
              <div style={{ fontSize: 12, color: C.textSub, marginBottom: 10 }}>
                Remove MBQ for <b style={{ fontFamily: 'monospace' }}>{delRow.st_cd}</b> / <b style={{ fontFamily: 'monospace' }}>{delRow.ref_art}</b>
                {' '}(<b>{delRow.mbq_q}</b>, {delRow.stream})? This is logged in Change Review.
              </div>
              <label style={{ fontSize: 10, fontWeight: 700, color: delReason.trim() ? C.textMuted : C.red, textTransform: 'uppercase', letterSpacing: '.03em' }}>
                Reason for deletion <span style={{ color: C.red }}>*</span>
              </label>
              <input autoFocus value={delReason} onChange={e => setDelReason(e.target.value)} disabled={deleting}
                placeholder="e.g. store closed, ref discontinued…" maxLength={500}
                className="input !py-1 !text-[12px]" style={{ width: '100%', marginTop: 3, borderColor: delReason.trim() ? undefined : C.red }} />
            </div>
            <div style={{ padding: '10px 16px', borderTop: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button onClick={() => setDelRow(null)} disabled={deleting} className="btn-secondary btn-sm">Cancel</button>
              <button onClick={onConfirmDelete} disabled={deleting || !delReason.trim()} className="btn-primary btn-sm"
                style={{ background: C.red, borderColor: C.red }}>
                {deleting ? 'Deleting…' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Upload confirmation — review before committing (safety) */}
      {preview && (() => {
        const d = preview.data || {}
        const badge = a => ({ CREATE: C.green, UPDATE: C.amber, UNCHANGED: C.textMuted }[a] || C.textSub)
        const cnt = [['New', d.created, C.green], ['Changed', d.updated, C.amber], ['Unchanged', d.unchanged, C.textMuted],
          ['Skipped', d.skipped, C.textMuted], ['Duplicates', d.duplicates, d.duplicates ? C.amber : C.textMuted],
          ['Errors', d.error_count, C.red], ['FA', d.fa, C.primary], ['CONS', d.cons, C.blue]]
        return (
          <div onClick={() => !committing && setPreview(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <div onClick={e => e.stopPropagation()} style={{ width: 780, maxWidth: '94vw', maxHeight: '86vh', display: 'flex', flexDirection: 'column', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
              <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ fontSize: 14, fontWeight: 700, color: C.text }}>Review upload · <span style={{ fontFamily: 'monospace', fontWeight: 400, fontSize: 12 }}>{preview.file?.name}</span></div>
                <button onClick={() => !committing && setPreview(null)} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><X size={18} color={C.textMuted} /></button>
              </div>
              <div style={{ padding: 12, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {cnt.map(([k, v, col]) => (
                  <div key={k} style={{ padding: '6px 12px', borderRadius: 8, border: `1px solid ${C.cardBorder}`, minWidth: 78 }}>
                    <div style={{ fontSize: 9, textTransform: 'uppercase', letterSpacing: '.03em', color: C.textMuted }}>{k}</div>
                    <div style={{ fontSize: 18, fontWeight: 800, color: col }}>{v ?? 0}</div>
                  </div>
                ))}
              </div>
              <div style={{ overflow: 'auto', flex: 1, borderTop: `1px solid ${C.cardBorder}` }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5 }}>
                  <thead><tr>{['Store', 'Ref Article', 'Stream', 'Action', 'Old MBQ', 'New MBQ'].map(h =>
                    <th key={h} style={{ ...th, textAlign: h.includes('MBQ') ? 'right' : 'left' }}>{h}</th>)}</tr></thead>
                  <tbody>
                    {(d.details || []).slice(0, 500).map((x, i) => (
                      <tr key={i} style={{ borderTop: `1px solid ${C.cardBorder}`, background: x.dup ? C.amberBg : 'transparent' }}>
                        <td style={{ padding: '4px 8px', fontFamily: 'monospace' }}>{x.st_cd}</td>
                        <td style={{ padding: '4px 8px', fontFamily: 'monospace' }}>
                          {x.ref_art}{x.dup && <span style={{ marginLeft: 6, fontSize: 9, fontWeight: 700, color: C.amber, border: `1px solid ${C.amberBd}`, background: C.card, borderRadius: 4, padding: '0 4px' }}>DUP</span>}
                        </td>
                        <td style={{ padding: '4px 8px' }}>{x.stream}</td>
                        <td style={{ padding: '4px 8px', fontWeight: 700, color: badge(x.action) }}>{x.action}</td>
                        <td style={{ padding: '4px 8px', textAlign: 'right', color: C.textMuted }}>{x.old_mbq ?? '—'}</td>
                        <td style={{ padding: '4px 8px', textAlign: 'right', fontWeight: 700 }}>{x.new_mbq}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {(d.details || []).length > 500 && <div style={{ padding: 8, textAlign: 'center', color: C.textMuted, fontSize: 11 }}>…and {d.details.length - 500} more</div>}
              </div>
              <div style={{ padding: '10px 16px', borderTop: `1px solid ${C.cardBorder}` }}>
                {(() => {
                  const hasChanges = (d.created + d.updated) > 0
                  const needReason = hasChanges && !uploadReason.trim()
                  return (<>
                    <div style={{ marginBottom: 8 }}>
                      <label style={{ fontSize: 10, fontWeight: 700, color: needReason ? C.red : C.textMuted, textTransform: 'uppercase', letterSpacing: '.03em' }}>
                        Reason for this change <span style={{ color: C.red }}>*</span>
                        <span style={{ fontWeight: 400, textTransform: 'none', color: C.textMuted }}> — required; recorded against every changed row for Change Review</span>
                      </label>
                      <input value={uploadReason} onChange={e => setUploadReason(e.target.value)} disabled={committing}
                        placeholder="e.g. Q3 range refresh, HC10 store expansion…"
                        className="input !py-1 !text-[12px]" style={{ width: '100%', marginTop: 3, borderColor: needReason ? C.red : undefined }} maxLength={500} />
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: 11, color: d.duplicates ? C.amber : d.error_count ? C.red : C.textMuted }}>
                        {d.duplicates
                          ? `⚠ ${d.duplicates} duplicate store×ref key(s) in the sheet (${d.dup_rows} extra row(s)) — the last value wins; fix the sheet if unintended.`
                          : d.error_count ? `${d.error_count} error row(s) will be skipped` : 'Nothing is saved until you confirm.'}
                      </span>
                      <div style={{ display: 'flex', gap: 8 }}>
                        <button onClick={() => setPreview(null)} disabled={committing} className="btn-secondary btn-sm">Cancel</button>
                        <button onClick={onConfirmUpload} disabled={committing || !hasChanges || needReason}
                          title={needReason ? 'Enter a reason for this change' : undefined} className="btn-primary btn-sm">
                          {committing ? 'Saving…' : `Confirm — apply ${d.created + d.updated} change(s)`}
                        </button>
                      </div>
                    </div>
                  </>)
                })()}
              </div>
            </div>
          </div>
        )
      })()}
      </>}
    </div>
  )
}
