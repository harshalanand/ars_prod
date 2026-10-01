import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Download, Trash2, RefreshCw, Search, ChevronLeft, ChevronRight, Check, XCircle, Lock, GitCompare } from 'lucide-react'
import toast from 'react-hot-toast'
import { b2bAPI } from '@/services/api'
import { C } from '@/theme/colors'
import B2BStepper from '@/components/b2b/B2BStepper'

const n = v => (v == null || v === '' ? '—' : Math.round(+v).toLocaleString())
const d2 = v => (v == null ? '—' : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }))
const when = s => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
const pct = (a, b) => (b ? `${((a / b) * 100).toFixed(1)}%` : '—')
const WH = { SAME: 'Own RDC only', HOME_FIRST: 'Own RDC first', ANY: 'All RDCs' }
const SHARE = (f, b) => (f === 'GREEDY' ? 'In priority order'
  : { PROPORTIONAL: 'Share — furthest from its own REQ', FLAT: 'Share — fewest units so far', NONE: 'Share — no re-order' }[b] || f)
const PRIORITY = { SHORTFALL_DESC: 'Biggest shortfall', CONT_DESC: 'Highest size share', MBQ_DESC: 'Biggest target' }
const LEFT_WORDS = {
  NO_SEASON_REQ: 'Every store asked for zero of it', NOT_IN_REQ: 'Category not in REQ (bags, hangers …)',
  NO_STORE_MASTER: 'Wanted only by stores missing from Store Master', NO_STORE_NEED: 'Wanted, but no store asked enough',
  NO_SHORTFALL: 'No store below its target', REQ_CAP_FULL: 'Stores were short, but their REQ was already filled',
  PARTIAL: 'Part of the bin moved; demand ran out first',
}
const TABS = [['picks', 'Pick list'], ['lines', 'Lines'], ['left', 'Leftovers'], ['why', 'Why no stock?'], ['settings', 'Settings used'], ['compare', 'Compare']]

export default function B2BSessionsPage() {
  const nav = useNavigate()
  const [qs, setQs] = useSearchParams()
  const [list, setList] = useState([])
  const [data, setData] = useState(null)
  const [why, setWhy] = useState(null)              // {store, art} to open in the Why tab
  const id = Number(qs.get('id')) || null
  const tab = qs.get('tab') || 'picks'
  const cmp = Number(qs.get('cmp')) || null
  const go = (patch) => { const q = new URLSearchParams(qs); Object.entries(patch).forEach(([k, v]) => (v == null ? q.delete(k) : q.set(k, v))); setQs(q, { replace: true }) }

  const loadList = useCallback(async () => {
    try {
      const { data: r } = await b2bAPI.sessions(60)
      setList(r.data.sessions)
      return r.data.sessions
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not load sessions') }
  }, [])
  const loadOne = useCallback(async (sid) => {
    if (!sid) { setData(null); return }
    try { const { data: r } = await b2bAPI.session(sid); setData(r.data) }
    catch (e) { toast.error(e.response?.data?.detail || 'Could not load the session') }
  }, [])

  useEffect(() => {
    loadList().then(rows => {
      if (!id && rows?.length) {
        const first = rows.find(s => s.STATUS === 'DONE' && !s.DRY_RUN) || rows[0]
        go({ id: first.SESSION_ID })
      }
    })
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { loadOne(id) }, [id, loadOne])

  const s = data?.session
  const stored = s && s.STATUS === 'DONE' && !s.DRY_RUN
  const locked = !s ? 'No session' : s.STATUS !== 'DONE' ? `Session is ${s.STATUS}` : s.DRY_RUN ? 'A dry run has no pick list'
    : !s.CHECKS_PASSED ? 'Locked: a balance check failed' : null

  const download = async (kind) => {
    const label = { xlsx: 'workbook', picks: 'pick list CSV', leftovers: 'leftovers CSV' }[kind]
    const t = toast.loading(`Preparing the ${label}${kind === 'xlsx' ? ' — about a minute the first time' : ''}…`)
    try {
      const res = await b2bAPI.exportSession(id, kind)
      const cd = res.headers['content-disposition'] || ''
      const name = (cd.match(/filename="?([^";]+)"?/) || [])[1] || `GRT_ALC_session_${id}.${kind === 'xlsx' ? 'xlsx' : 'csv'}`
      const url = URL.createObjectURL(res.data)
      const a = document.createElement('a'); a.href = url; a.download = name; a.click()
      setTimeout(() => URL.revokeObjectURL(url), 5000)
      toast.success(`Downloaded ${name}`, { id: t })
    } catch (e) {
      let msg = 'Export failed'
      try { msg = JSON.parse(await e.response.data.text()).detail || msg } catch { /* not JSON */ }
      toast.error(msg, { id: t })
    }
  }
  const remove = async () => {
    const typed = window.prompt(`Delete session ${id} from all three tables? Type DELETE to confirm.`)
    if (typed !== 'DELETE') return
    try {
      const { data: r } = await b2bAPI.deleteSession(id)
      toast.success(r.message)
      const rows = await loadList()
      go({ id: rows?.[0]?.SESSION_ID ?? null, cmp: null })
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not delete') }
  }

  const others = useMemo(() => list.filter(x => x.SESSION_ID !== id && x.STATUS === 'DONE' && !x.DRY_RUN), [list, id])

  return (
    <div className="space-y-3">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title">5 · Sessions & Pick List</h1>
          <p className="page-subtitle">Every run, its checks, the pick list, what stayed in the bins, and why a store got nothing.</p>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <button type="button" className="btn-secondary" onClick={() => { loadList(); loadOne(id) }}><RefreshCw size={12} /> Refresh</button>
          <button type="button" className="btn-secondary" onClick={() => nav('/bin-alloc/run')}>New run</button>
        </div>
      </div>
      <B2BStepper current="sessions" />

      <div className="card" style={{ padding: 10, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <select className="input" value={id || ''} onChange={e => go({ id: e.target.value, cmp: null })} style={{ minWidth: 380, flex: '1 1 380px' }}>
          {!list.length && <option value="">No sessions yet</option>}
          {list.map(x => (
            <option key={x.SESSION_ID} value={x.SESSION_ID}>
              #{x.SESSION_ID} · {when(x.CREATED_AT)} · {x.DRY_RUN ? 'dry run · ' : ''}{SHARE(x.ALLOC_FILL_MODE, x.ALLOC_FAIR_BASIS)} · {WH[x.ALLOC_CROSS_RDC] || x.ALLOC_CROSS_RDC} · {n(x.UNITS_ALLOCATED)} units{x.STATUS !== 'DONE' ? ` · ${x.STATUS}` : ''}
            </option>
          ))}
        </select>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <GitCompare size={13} style={{ color: C.textSub }} />
          <select className="input" value={cmp || ''} onChange={e => go({ cmp: e.target.value || null, tab: e.target.value ? 'compare' : tab })}
            disabled={!stored || !others.length} style={{ width: 200 }}>
            <option value="">Compare with…</option>
            {others.map(x => <option key={x.SESSION_ID} value={x.SESSION_ID}>#{x.SESSION_ID} · {SHARE(x.ALLOC_FILL_MODE, x.ALLOC_FAIR_BASIS)} · {WH[x.ALLOC_CROSS_RDC]}</option>)}
          </select>
        </span>
        <span title={locked || ''} style={{ display: 'inline-flex', gap: 4 }}>
          <button type="button" className="btn-primary btn-sm" disabled={!!locked} onClick={() => download('xlsx')}>
            {locked ? <Lock size={11} /> : <Download size={11} />} Pick-list workbook
          </button>
          <button type="button" className="btn-secondary btn-sm" disabled={!!locked} onClick={() => download('picks')}>Pick list CSV</button>
          <button type="button" className="btn-secondary btn-sm" disabled={!!locked} onClick={() => download('leftovers')}>Leftovers CSV</button>
        </span>
        <button type="button" className="btn-secondary btn-sm" disabled={!s || s.STATUS === 'RUNNING'} onClick={remove} title="Delete this session">
          <Trash2 size={11} /> Delete
        </button>
      </div>

      {s && <Header data={data} locked={locked} />}
      {s && stored && (
        <div className="card">
          <div style={{ display: 'flex', borderBottom: `1px solid ${C.cardBorder}`, overflowX: 'auto' }}>
            {TABS.filter(([k]) => k !== 'compare' || cmp).map(([k, label]) => (
              <button key={k} type="button" onClick={() => go({ tab: k })}
                style={{ padding: '8px 14px', border: 'none', background: 'none', cursor: 'pointer', fontSize: 11, fontWeight: 700,
                         whiteSpace: 'nowrap', color: tab === k ? C.primary : C.textSub,
                         borderBottom: `2px solid ${tab === k ? C.primary : 'transparent'}` }}>{label}</button>
            ))}
          </div>
          <div style={{ padding: 10 }}>
            {tab === 'picks' && <Picks sid={id} />}
            {tab === 'lines' && <Lines sid={id} onWhy={(st, art) => { setWhy({ store: st, art }); go({ tab: 'why' }) }} />}
            {tab === 'left' && <Leftovers sid={id} summary={data.leftovers || []} />}
            {tab === 'why' && <Why sid={id} start={why} ctx={data.context} />}
            {tab === 'settings' && <Settings s={s} />}
            {tab === 'compare' && cmp && <Compare a={id} b={cmp} />}
          </div>
        </div>
      )}
      {s && !stored && (
        <div className="card" style={{ padding: 12, fontSize: 11, color: C.textSub }}>
          {s.DRY_RUN ? 'This is a dry run: it was computed and checked, but no lines or picks were written. Its totals are above.' :
            `This session is ${s.STATUS}. ${s.progress?.text || ''} ${String(s.ERROR || '').split('\n')[0]}`}
        </div>
      )}
    </div>
  )
}

function Header({ data, locked }) {
  const s = data.session
  const flow = data.rdc_flow || []
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.4fr) minmax(0, .6fr)', gap: 10 }}>
      <div className="card" style={{ padding: 10, display: 'grid', gap: 8 }}>
        <div style={{ fontSize: 11.5 }}>
          <b>Session {s.SESSION_ID}</b> · {when(s.CREATED_AT)} · {s.CREATED_BY} · {s.DRY_RUN ? 'dry run · ' : ''}
          {SHARE(s.ALLOC_FILL_MODE, s.ALLOC_FAIR_BASIS)} · {WH[s.ALLOC_CROSS_RDC] || s.ALLOC_CROSS_RDC} · {PRIORITY[s.ALLOC_PRIORITY] || s.ALLOC_PRIORITY} ·
          min {s.ALLOC_MIN_QTY} · {s.ALLOC_BIN_PICK === 'MAX_CONSUMPTION' ? 'fewest picks' : 'bin order'} · build {s.BUILD_ID}
          {s.NOTE && <span style={{ color: C.textMuted }}> · “{s.NOTE}”</span>}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: 8 }}>
          <Mini k="Allocated" v={n(s.UNITS_ALLOCATED)} x={`${pct(s.UNITS_ALLOCATED, s.SUPPLY_UNITS)} of ${n(s.SUPPLY_UNITS)}`} />
          <Mini k="Left in bins" v={n(s.UNITS_LEFT)} />
          <Mini k="Lines" v={n(s.ART_LINES)} />
          <Mini k="Pick rows" v={n(s.BIN_LINES)} />
          <Mini k="Stores served" v={n(s.STORES_SERVED)} x={data.stores_without ? `${data.stores_without} got nothing` : null} />
          <Mini k="Cross-warehouse" v={n(s.CROSS_RDC_UNITS)} x={pct(s.CROSS_RDC_UNITS, s.UNITS_ALLOCATED)} tone={s.CROSS_RDC_UNITS > 0 ? 'amber' : null} />
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {(s.checks || []).map(c => (
            <span key={c.code} title={c.detail || c.title}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10.5, padding: '3px 8px', borderRadius: 999,
                       background: c.level === 'ok' ? C.greenBg : C.redBg, color: c.level === 'ok' ? C.green : C.red, fontWeight: 700 }}>
              {c.level === 'ok' ? <Check size={11} /> : <XCircle size={11} />} {c.title}
            </span>
          ))}
          {locked && <span style={{ fontSize: 10.5, color: C.textMuted, alignSelf: 'center' }}>Export: {locked}</span>}
        </div>
        {data.context && !data.context.current && (
          <div style={{ fontSize: 10.5, color: C.amber }}>{data.context.why.join(' ')} “Why no stock?” cannot replay this session exactly.</div>
        )}
      </div>
      <div className="card" style={{ padding: 10 }}>
        <Sub>Where the stock went</Sub>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
          <thead><tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
            <th style={{ textAlign: 'left' }}>From bins in</th><th style={{ textAlign: 'left' }}>To stores of</th><th style={{ textAlign: 'right' }}>Units</th><th style={{ textAlign: 'right' }}>Stores</th></tr></thead>
          <tbody>
            {flow.map((f, i) => (
              <tr key={i} style={{ borderTop: `1px solid ${C.cardBorder}`, color: f.BIN_RDC !== f.STORE_RDC ? C.amber : C.text }}>
                <td style={{ padding: '3px 0' }}>{f.BIN_RDC}</td><td>{f.STORE_RDC}{f.BIN_RDC !== f.STORE_RDC ? ' · cross' : ''}</td>
                <td style={{ textAlign: 'right' }}>{n(f.UNITS)}</td><td style={{ textAlign: 'right' }}>{n(f.STORES)}</td>
              </tr>
            ))}
            {!flow.length && <tr><td colSpan={4} style={{ color: C.textMuted, padding: 4 }}>No picks</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/* ── a paged, filtered table over one of the session endpoints ───────────── */
function Paged({ fetch, cols, filters, initial, onRow, rowTone, deps = [] }) {
  const [p, setP] = useState(initial)
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const run = useCallback(async (q) => {
    setBusy(true)
    try { const { data: r } = await fetch(Object.fromEntries(Object.entries(q).filter(([, v]) => v !== '' && v != null && v !== false))); setRes(r.data) }
    catch (e) { toast.error(e.response?.data?.detail || 'Could not load') }
    finally { setBusy(false) }
  }, [fetch])
  useEffect(() => { run({ ...initial, page: 1 }); setP({ ...initial, page: 1 }) }, deps)  // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k, v) => setP(o => ({ ...o, [k]: v }))
  const pages = res ? Math.max(1, Math.ceil(res.total / (res.size || 50))) : 1
  const page = (pg) => { const q = { ...p, page: pg }; setP(q); run(q) }
  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <form onSubmit={e => { e.preventDefault(); page(1) }} style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {filters.map(f => f.type === 'select' ? (
          <select key={f.key} className="input" value={p[f.key] ?? ''} onChange={e => set(f.key, e.target.value)} style={{ width: f.w || 140 }}>
            {f.options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        ) : f.type === 'check' ? (
          <label key={f.key} style={{ display: 'flex', gap: 5, fontSize: 11, alignItems: 'center' }}>
            <input type="checkbox" checked={!!p[f.key]} onChange={e => set(f.key, e.target.checked)} />{f.label}
          </label>
        ) : (
          <input key={f.key} className="input" placeholder={f.label} value={p[f.key] ?? ''} onChange={e => set(f.key, e.target.value)} style={{ width: f.w || 110 }} />
        ))}
        <button type="submit" className="btn-primary btn-sm" disabled={busy}><Search size={11} /> Show</button>
        <span style={{ marginLeft: 'auto', fontSize: 10.5, color: C.textMuted }}>{res ? `${n(res.total)} rows` : ''}</span>
      </form>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums', opacity: busy ? 0.5 : 1 }}>
          <thead><tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
            {cols.map(c => <th key={c.k} style={{ padding: '5px 7px', textAlign: c.right ? 'right' : 'left', whiteSpace: 'nowrap' }}>{c.l}</th>)}
          </tr></thead>
          <tbody>
            {(res?.rows || []).map((r, i) => (
              <tr key={i} onClick={onRow ? () => onRow(r) : undefined}
                style={{ borderTop: `1px solid ${C.cardBorder}`, cursor: onRow ? 'pointer' : undefined, background: rowTone ? rowTone(r) : undefined }}>
                {cols.map(c => (
                  <td key={c.k} style={{ padding: '4px 7px', textAlign: c.right ? 'right' : 'left', whiteSpace: 'nowrap', fontWeight: c.bold ? 700 : 400 }}>
                    {c.f ? c.f(r[c.k], r) : (r[c.k] ?? '—')}
                  </td>
                ))}
              </tr>
            ))}
            {res && !res.rows.length && <tr><td colSpan={cols.length} style={{ padding: 12, color: C.textMuted }}>No rows match</td></tr>}
          </tbody>
        </table>
      </div>
      {res && res.total > (res.size || 50) && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 10.5 }}>
          <button type="button" className="btn-secondary btn-sm" disabled={p.page <= 1 || busy} onClick={() => page(p.page - 1)}><ChevronLeft size={11} /></button>
          <span>Page {n(p.page)} of {n(pages)}</span>
          <button type="button" className="btn-secondary btn-sm" disabled={p.page >= pages || busy} onClick={() => page(p.page + 1)}><ChevronRight size={11} /></button>
        </div>
      )}
    </div>
  )
}

function Picks({ sid }) {
  const fetch = useCallback(q => b2bAPI.sessionPicks(sid, q), [sid])
  return (
    <>
      <div style={{ fontSize: 10.5, color: C.textSub, marginBottom: 6 }}>
        In warehouse walking order — warehouse, bin, article, then the order stores draw from that bin. <b>Bin left</b> counts the bin down to what stays in it.
      </div>
      <Paged fetch={fetch} deps={[sid]} initial={{ sort: 'walk', cross: false }}
        filters={[{ key: 'store', label: 'Store', w: 80 }, { key: 'bin', label: 'Bin', w: 130 }, { key: 'art', label: 'Article', w: 130 },
                  { key: 'maj_cat', label: 'Category', w: 120 }, { key: 'rdc', label: 'Bin RDC', w: 80 },
                  { key: 'cross', type: 'check', label: 'Cross-warehouse only' },
                  { key: 'sort', type: 'select', options: [['walk', 'Walking order'], ['store', 'By store'], ['qty', 'Biggest first']] }]}
        rowTone={r => (r.CROSS ? C.amberBg : undefined)}
        cols={[{ k: 'BIN', l: 'Bin', bold: true }, { k: 'BIN_RDC', l: 'Bin RDC' }, { k: 'ART', l: 'Article' }, { k: 'MAJ_CAT', l: 'Category' },
               { k: 'BIN_SIZE', l: 'Size' }, { k: 'STORE_CODE', l: 'Store', bold: true }, { k: 'ST_NM', l: 'Store name' },
               { k: 'STORE_RDC', l: 'Store RDC', f: (v, r) => (r.CROSS ? `${v} · cross` : v) }, { k: 'QTY', l: 'Qty', right: true, bold: true },
               { k: 'PICK_SEQ', l: 'Pick #', right: true }, { k: 'BIN_QTY', l: 'Bin held', right: true }, { k: 'BIN_QTY_LEFT', l: 'Bin left', right: true }]} />
    </>
  )
}

function Lines({ sid, onWhy }) {
  const fetch = useCallback(q => b2bAPI.sessionLines(sid, q), [sid])
  return (
    <>
      <div style={{ fontSize: 10.5, color: C.textSub, marginBottom: 6 }}>
        One row per store × article that received stock, in the order the run served them. Click a row for its full story in “Why no stock?”.
      </div>
      <Paged fetch={fetch} deps={[sid]} initial={{ sort: 'seq' }} onRow={r => onWhy(r.STORE_CODE, r.ART)}
        filters={[{ key: 'store', label: 'Store', w: 80 }, { key: 'art', label: 'Article', w: 130 }, { key: 'maj_cat', label: 'Category', w: 120 },
                  { key: 'sort', type: 'select', options: [['seq', 'Served first'], ['qty', 'Biggest lines'], ['store', 'By store'], ['sendable', 'Most still sendable']] }]}
        cols={[{ k: 'ALLOC_SEQ', l: '#', right: true }, { k: 'STORE_CODE', l: 'Store', bold: true }, { k: 'ART', l: 'Article' }, { k: 'MAJ_CAT', l: 'Category' },
               { k: 'BIN_SIZE', l: 'Size' }, { k: 'MBQ_ROUNDED', l: 'Target', right: true }, { k: 'STK_TTL', l: 'Stock', right: true, f: d2 },
               { k: 'SHORTFALL', l: 'Short', right: true, f: d2 }, { k: 'REQ_CAP', l: 'REQ cap', right: true },
               { k: 'ALLOC_QTY', l: 'Sent', right: true, bold: true }, { k: 'ART_BIN_LEFT', l: 'Article left', right: true },
               { k: 'STILL_SENDABLE', l: 'Still sendable', right: true, f: d2 }]} />
    </>
  )
}

function Leftovers({ sid, summary }) {
  const [reason, setReason] = useState('')
  const fetch = useCallback(q => b2bAPI.sessionLeft(sid, q), [sid])
  const total = summary.reduce((a, r) => a + (r.PCS || 0), 0)
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 8 }}>
        {summary.map(r => (
          <button key={r.LEFT_REASON} type="button" onClick={() => setReason(reason === r.LEFT_REASON ? '' : r.LEFT_REASON)}
            style={{ textAlign: 'left', cursor: 'pointer', padding: '7px 9px', borderRadius: 6,
                     border: `1px solid ${reason === r.LEFT_REASON ? C.primary : C.cardBorder}`, background: reason === r.LEFT_REASON ? C.primaryLt : C.card }}>
            <div style={{ fontSize: 9.5, fontWeight: 700, color: C.textMuted, letterSpacing: '.05em' }}>{r.LEFT_REASON}</div>
            <div style={{ fontSize: 15, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>{n(r.PCS)} pcs <span style={{ fontSize: 10, color: C.textMuted, fontWeight: 400 }}>{pct(r.PCS, total)}</span></div>
            <div style={{ fontSize: 10, color: C.textSub }}>{LEFT_WORDS[r.LEFT_REASON] || ''} · {n(r.BINS)} bin rows</div>
          </button>
        ))}
      </div>
      <Paged key={reason} fetch={fetch} deps={[sid, reason]} initial={{ reason }}
        filters={[{ key: 'maj_cat', label: 'Category', w: 120 }, { key: 'bin', label: 'Bin', w: 130 }, { key: 'art', label: 'Article', w: 130 }]}
        cols={[{ k: 'BIN', l: 'Bin', bold: true }, { k: 'BIN_RDC', l: 'RDC' }, { k: 'ART', l: 'Article' }, { k: 'MAJ_CAT', l: 'Category' },
               { k: 'BIN_SIZE', l: 'Size' }, { k: 'BIN_QTY', l: 'Held', right: true }, { k: 'PICKED_QTY', l: 'Picked', right: true },
               { k: 'QTY', l: 'Left', right: true, bold: true }, { k: 'LEFT_REASON', l: 'Why', f: v => <span title={LEFT_WORDS[v]}>{v}</span> },
               { k: 'STORES_WANTING', l: 'Stores short', right: true }, { k: 'TOTAL_SHORTFALL', l: 'Their shortfall', right: true, f: n }]} />
    </div>
  )
}

function Why({ sid, start, ctx }) {
  const [store, setStore] = useState(start?.store || '')
  const [art, setArt] = useState(start?.art || '')
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const run = useCallback(async (st, a) => {
    if (!st) return
    setBusy(true)
    try { const { data: r } = await b2bAPI.sessionWhy(sid, st, a); setRes(r.data) }
    catch (e) { toast.error(e.response?.data?.detail || 'Could not walk it') }
    finally { setBusy(false) }
  }, [sid])
  useEffect(() => { if (start?.store) { setStore(start.store); setArt(start.art || ''); run(start.store, start.art) } }, [start, run])
  return (
    <div style={{ display: 'grid', gap: 10, maxWidth: 980 }}>
      <form onSubmit={e => { e.preventDefault(); run(store.trim(), art.trim()) }} style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
        <input className="input" placeholder="Store" value={store} onChange={e => setStore(e.target.value)} style={{ width: 90 }} />
        <input className="input" placeholder="Article (optional)" value={art} onChange={e => setArt(e.target.value)} style={{ width: 170 }} />
        <button type="submit" className="btn-primary btn-sm" disabled={busy || !store.trim()}><Search size={11} /> Walk it</button>
        <span style={{ fontSize: 10.5, color: C.textMuted }}>
          {ctx?.current ? 'The session’s data is still current, so an article it got none of is replayed exactly — its category is re-run with a tracer.'
            : 'The data has changed since this session: stored rows are shown, but its turn cannot be replayed.'}
        </span>
      </form>
      {busy && <div style={{ fontSize: 11, color: C.textSub }}>Walking it… (replaying a category takes a few seconds)</div>}
      {res && !busy && (
        <div style={{ display: 'grid', gap: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 700, lineHeight: 1.45 }}>{res.verdict}</div>
          {res.steps.map((s, i) => (
            <div key={i} style={{ display: 'grid', gridTemplateColumns: '16px minmax(0, 220px) minmax(0, 1fr)', gap: 8, fontSize: 11, alignItems: 'start' }}>
              {s.ok ? <Check size={13} style={{ color: C.green, marginTop: 1 }} /> : <XCircle size={13} style={{ color: C.red, marginTop: 1 }} />}
              <span style={{ color: C.textSub }}>{s.label}<div style={{ fontWeight: 700, color: C.text }}>{s.value}</div></span>
              <span style={{ color: C.textMuted, lineHeight: 1.45 }}>{s.detail}</span>
            </div>
          ))}
          {res.misses?.length > 0 && (
            <div>
              <Sub>Its biggest shortfalls that got nothing — click one to see why</Sub>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
                <thead><tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
                  {['Article', 'Category', 'Size', 'Target', 'Stock', 'Short', 'In bins'].map((h, i) => <th key={h} style={{ textAlign: i > 2 ? 'right' : 'left', padding: '3px 6px' }}>{h}</th>)}
                </tr></thead>
                <tbody>
                  {res.misses.map(m => (
                    <tr key={m.ART} onClick={() => { setArt(m.ART); run(store.trim(), m.ART) }} style={{ borderTop: `1px solid ${C.cardBorder}`, cursor: 'pointer' }}>
                      <td style={{ padding: '3px 6px', fontWeight: 700 }}>{m.ART}</td><td style={{ padding: '3px 6px' }}>{m.MAJ_CAT}</td><td style={{ padding: '3px 6px' }}>{m.BIN_SIZE}</td>
                      <td style={{ padding: '3px 6px', textAlign: 'right' }}>{m.MBQ_ROUNDED}</td><td style={{ padding: '3px 6px', textAlign: 'right' }}>{d2(m.STK_TTL)}</td>
                      <td style={{ padding: '3px 6px', textAlign: 'right', fontWeight: 700 }}>{d2(m.SHORTFALL)}</td><td style={{ padding: '3px 6px', textAlign: 'right' }}>{n(m.BIN_QTY)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Settings({ s }) {
  const st = s.settings || {}
  const rows = [
    ['How stock is shared', SHARE(s.ALLOC_FILL_MODE, s.ALLOC_FAIR_BASIS)], ['Which warehouse may ship', WH[s.ALLOC_CROSS_RDC] || s.ALLOC_CROSS_RDC],
    ['Who is served first', PRIORITY[s.ALLOC_PRIORITY] || s.ALLOC_PRIORITY], ['Smallest line', s.ALLOC_MIN_QTY],
    ['Which bin', s.ALLOC_BIN_PICK === 'MAX_CONSUMPTION' ? 'Fewest picks' : 'Bin order'], ['Demand build', s.BUILD_ID],
    ['Upload the build read', st.upload_id], ['Workers', s.WORKERS], ['Run by', s.CREATED_BY], ['Run at', when(s.CREATED_AT)],
    ['Took', `${Math.round(s.DURATION_SEC || 0)} s` + (s.steps ? ` (allocate ${s.steps.allocate ?? '—'} s, write ${s.steps.write ?? '—'} s)` : '')],
    ['Note', s.NOTE || '—'],
  ]
  const skips = s.skips || {}
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 14 }}>
      <table style={{ borderCollapse: 'collapse', fontSize: 11 }}>
        <tbody>{rows.map(([k, v]) => (
          <tr key={k} style={{ borderTop: `1px solid ${C.cardBorder}` }}><td style={{ padding: '4px 8px', color: C.textSub }}>{k}</td><td style={{ padding: '4px 8px', fontWeight: 700 }}>{v ?? '—'}</td></tr>
        ))}</tbody>
      </table>
      <div>
        <Sub>Candidates looked at and skipped (lines, not units)</Sub>
        <table style={{ borderCollapse: 'collapse', fontSize: 11, width: '100%', fontVariantNumeric: 'tabular-nums' }}>
          <tbody>
            {[['candidates', 'Candidates'], ['blocked_art_empty', 'Article had run out'], ['blocked_req_full', 'REQ already filled'],
              ['blocked_no_req', 'No REQ line'], ['below_min_qty', 'Below the smallest line'], ['blocked_no_rdc', 'Not in an allowed warehouse'],
              ['rr_passes', 'Sharing passes'], ['cross_rdc_units', 'Units drawn cross-warehouse']].map(([k, l]) => (
              <tr key={k} style={{ borderTop: `1px solid ${C.cardBorder}` }}><td style={{ padding: '4px 8px', color: C.textSub }}>{l}</td><td style={{ padding: '4px 8px', textAlign: 'right' }}>{n(skips[k])}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Compare({ a, b }) {
  const [res, setRes] = useState(null)
  useEffect(() => {
    setRes(null)
    b2bAPI.compareSessions(a, b).then(({ data: r }) => setRes(r.data)).catch(e => toast.error(e.response?.data?.detail || 'Could not compare'))
  }, [a, b])
  if (!res) return <div style={{ fontSize: 11, color: C.textMuted }}>Comparing…</div>
  const A = res.a, B = res.b
  const row = (label, ka, fmt = n) => {
    const va = A[ka], vb = B[ka]
    const d = (vb ?? 0) - (va ?? 0)
    return (
      <tr key={label} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
        <td style={{ padding: '4px 8px', color: C.textSub }}>{label}</td>
        <td style={{ padding: '4px 8px', textAlign: 'right' }}>{fmt(va)}</td><td style={{ padding: '4px 8px', textAlign: 'right' }}>{fmt(vb)}</td>
        <td style={{ padding: '4px 8px', textAlign: 'right', color: d > 0 ? C.green : d < 0 ? C.red : C.textMuted }}>{typeof va === 'number' ? (d > 0 ? '+' : '') + n(d) : ''}</td>
      </tr>
    )
  }
  const storeTable = (rows, title) => (
    <div>
      <Sub>{title}</Sub>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
        <tbody>{rows.map(r => (
          <tr key={r.STORE_CODE} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
            <td style={{ padding: '3px 6px', fontWeight: 700 }}>{r.STORE_CODE}</td><td style={{ padding: '3px 6px', color: C.textSub }}>{r.ST_NM} · {r.RDC}</td>
            <td style={{ padding: '3px 6px', textAlign: 'right' }}>{n(r.A)} → {n(r.B)}</td>
            <td style={{ padding: '3px 6px', textAlign: 'right', color: r.DELTA > 0 ? C.green : C.red, fontWeight: 700 }}>{r.DELTA > 0 ? '+' : ''}{n(r.DELTA)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
  return (
    <div style={{ display: 'grid', gap: 12 }}>
      {!res.same_build && <div style={{ fontSize: 11, color: C.amber }}>These sessions read different demand builds ({A.BUILD_ID} and {B.BUILD_ID}), so the difference is not only the settings.</div>}
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 14 }}>
        <table style={{ borderCollapse: 'collapse', fontSize: 11, fontVariantNumeric: 'tabular-nums' }}>
          <thead><tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
            <th></th><th style={{ textAlign: 'right' }}>#{A.SESSION_ID}</th><th style={{ textAlign: 'right' }}>#{B.SESSION_ID}</th><th style={{ textAlign: 'right' }}>Change</th></tr></thead>
          <tbody>
            <tr><td style={{ padding: '4px 8px', color: C.textSub }}>Sharing · warehouse</td>
              <td style={{ padding: '4px 8px', textAlign: 'right' }}>{SHARE(A.ALLOC_FILL_MODE, A.ALLOC_FAIR_BASIS)} · {WH[A.ALLOC_CROSS_RDC]}</td>
              <td style={{ padding: '4px 8px', textAlign: 'right' }}>{SHARE(B.ALLOC_FILL_MODE, B.ALLOC_FAIR_BASIS)} · {WH[B.ALLOC_CROSS_RDC]}</td><td></td></tr>
            {row('Units allocated', 'UNITS_ALLOCATED')}{row('Left in bins', 'UNITS_LEFT')}{row('Lines', 'ART_LINES')}
            {row('Pick rows', 'BIN_LINES')}{row('Stores served', 'STORES_SERVED')}{row('Cross-warehouse units', 'CROSS_RDC_UNITS')}
          </tbody>
        </table>
        <div style={{ fontSize: 11, color: C.textSub, lineHeight: 1.6 }}>
          <Sub>Store × article lines</Sub>
          <div>Same quantity in both: <b>{n(res.lines.same)}</b></div>
          <div>Quantity changed: <b>{n(res.lines.qty_changed)}</b></div>
          <div>Only in #{A.SESSION_ID}: <b>{n(res.lines.only_a)}</b> · only in #{B.SESSION_ID}: <b>{n(res.lines.only_b)}</b></div>
          <div style={{ marginTop: 6 }}>{n(res.stores_changed)} stores receive a different total · {n(res.stores_gained_any)} get stock only in #{B.SESSION_ID} · {n(res.stores_lost_all)} only in #{A.SESSION_ID}</div>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 14 }}>
        {storeTable(res.stores_up, `Stores that gain most in #${B.SESSION_ID}`)}
        {storeTable(res.stores_down, `Stores that lose most in #${B.SESSION_ID}`)}
      </div>
      <div>
        <Sub>Categories that change most</Sub>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
          <tbody>{res.categories.map(c => (
            <tr key={c.MAJ_CAT} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
              <td style={{ padding: '3px 6px', fontWeight: 700 }}>{c.MAJ_CAT}</td>
              <td style={{ padding: '3px 6px', textAlign: 'right' }}>{n(c.A)} → {n(c.B)}</td>
              <td style={{ padding: '3px 6px', textAlign: 'right', color: c.DELTA > 0 ? C.green : C.red, fontWeight: 700 }}>{c.DELTA > 0 ? '+' : ''}{n(c.DELTA)}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  )
}

function Sub({ children }) {
  return <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: C.textMuted, marginBottom: 5 }}>{children}</div>
}

function Mini({ k, v, x, tone }) {
  return (
    <div style={{ border: `1px solid ${tone === 'amber' ? C.amberBd : C.cardBorder}`, borderRadius: 6, padding: '6px 9px' }}>
      <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: C.textMuted }}>{k}</div>
      <div style={{ fontSize: 15, fontWeight: 800, fontVariantNumeric: 'tabular-nums', color: tone === 'amber' ? C.amber : C.text }}>{v}</div>
      {x && <div style={{ fontSize: 10, color: C.textSub }}>{x}</div>}
    </div>
  )
}
