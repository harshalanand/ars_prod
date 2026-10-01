import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Hammer, RefreshCw, X, Search, ChevronLeft, ChevronRight, Check, XCircle, Settings2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { b2bAPI } from '@/services/api'
import { C } from '@/theme/colors'
import B2BStepper from '@/components/b2b/B2BStepper'
import CheckList from '@/components/b2b/CheckList'

const n = v => (v == null ? '—' : Math.round(+v).toLocaleString())
const d2 = v => (v == null ? '—' : (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }))
const when = s => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
const pct = (a, b) => (b ? `${((a / b) * 100).toFixed(1)}%` : '—')
const LABEL = {
  SHORT_DAYS: 'Norm days, fast sizes', LONG_DAYS: 'Norm days, other sizes', SHORT_SZ_LIST: 'Fast sizes',
  TREAT_MAJCAT_MIX_AS_SHORT: 'MIX categories use fast days', DEFAULT_SALE_COVER_DAYS: 'Sale cover days',
  TREAT_MISSING_STK_AS_ZERO: 'No grid row = zero stock', CONT_SOURCE_MODE: 'Size share source',
  CONT_APPLY: 'Scale by size share', CONT_FULL_SZ_LIST: 'Sizes forced to 100%', CONT_FALLBACK: 'Share when no row',
  MBQ_MIN_WHEN_CONT: 'Smallest target', MBQ_PRUNE_TO_REQ: 'Only rows a store can take', MBQ_MIN_REQ_UNITS: 'Minimum REQ',
}
const BOOLS = ['TREAT_MAJCAT_MIX_AS_SHORT', 'TREAT_MISSING_STK_AS_ZERO', 'CONT_APPLY', 'MBQ_PRUNE_TO_REQ']
const fmtSetting = (k, v) => (BOOLS.includes(k) ? (v === '1' ? 'On' : v === '0' ? 'Off' : v) : v)
// 0.12%, 4.8%, 37% — enough digits to tell small covers apart
const cov = x => (x == null ? '—' : `${x < 1 ? x.toFixed(2) : x < 10 ? x.toFixed(1) : x.toFixed(0)}%`)

export default function B2BMbqPage() {
  const nav = useNavigate()
  const [st, setSt] = useState(null)            // GET /b2b/mbq
  const [job, setJob] = useState(null)          // the running build
  const [busy, setBusy] = useState(false)
  const poll = useRef(null)

  const load = useCallback(async () => {
    try {
      const { data: r } = await b2bAPI.mbq()
      setSt(r.data)
      return r.data
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not load the build status') }
  }, [])

  const watch = useCallback((id) => {
    clearInterval(poll.current)
    const tick = async () => {
      try {
        const { data: r } = await b2bAPI.mbqBuild(id)
        setJob(r.data)
        if (r.data.STATUS !== 'RUNNING') {
          clearInterval(poll.current)
          if (r.data.STATUS === 'DONE') toast.success(r.data.PROGRESS)
          else if (r.data.STATUS === 'CANCELLED') toast(r.data.PROGRESS)
          else toast.error(r.data.PROGRESS || 'Build failed')
          setJob(null)
          load()
        }
      } catch { clearInterval(poll.current) }
    }
    tick()
    poll.current = setInterval(tick, 1500)
  }, [load])

  useEffect(() => {
    load().then(d => { if (d?.running) watch(d.running.BUILD_ID) })     // resume after a refresh
    return () => clearInterval(poll.current)
  }, [load, watch])

  const build = async () => {
    setBusy(true)
    try {
      const { data: r } = await b2bAPI.buildMbq()
      toast(r.message)
      watch(r.data.build_id)
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not start the build') }
    finally { setBusy(false) }
  }
  const cancel = async () => {
    if (!job) return
    try { const { data: r } = await b2bAPI.cancelBuild(job.BUILD_ID); toast(r.message) } catch { /* ignore */ }
  }

  const L = st?.latest
  const s = L?.summary
  const f = st?.freshness
  const running = !!job || !!st?.running
  const cover = s && s.shortfall ? (s.bin_pcs / s.shortfall) * 100 : null

  return (
    <div className="space-y-3">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title">3 · Build MBQ</h1>
          <p className="page-subtitle">A target and a shortfall for every store × article. Allocation can only fill the shortfall.</p>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <button type="button" className="btn-secondary" onClick={load}><RefreshCw size={12} /> Refresh</button>
          <button type="button" className="btn-secondary" onClick={() => nav('/bin-alloc/settings')}>
            <Settings2 size={12} /> Settings
          </button>
          <button type="button" className="btn-primary" onClick={build} disabled={running || busy}>
            <Hammer size={12} /> {L ? 'Rebuild MBQ' : 'Build MBQ'}
          </button>
        </div>
      </div>
      <B2BStepper current="mbq" />

      {job && (
        <div className="card" style={{ padding: 12, display: 'grid', gap: 7 }}>
          <div style={{ display: 'flex', fontSize: 11, gap: 8 }}>
            <b>Build {job.BUILD_ID}</b><span style={{ color: C.textSub }}>{job.PROGRESS}</span>
            <span style={{ marginLeft: 'auto', color: C.textMuted }}>{job.PROGRESS_PCT || 0}%</span>
          </div>
          <div style={{ height: 7, borderRadius: 4, background: C.grayBd, overflow: 'hidden' }}>
            <div style={{ width: `${job.PROGRESS_PCT || 0}%`, height: '100%', background: C.primary, transition: 'width .4s' }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <button type="button" className="btn-secondary btn-sm" onClick={cancel}><X size={11} /> Cancel</button>
            <span style={{ fontSize: 10.5, color: C.textSub }}>
              The rows go into a staging table and are checked there. The current demand stays in use until the new one
              passes and is switched in — a cancel or a failure changes nothing.
            </span>
          </div>
        </div>
      )}

      {f && !job && <Freshness f={f} latest={L} onBuild={build} disabled={running || busy} />}

      {s && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: 8 }}>
          <Tile k="Demand rows" v={n(s.rows)} x={`${n(s.stores)} stores × ${n(s.articles)} articles`} />
          <Tile k="Rows short" v={n(s.rows_short)} x={`${pct(s.rows_short, s.rows)} of rows`} />
          <Tile k="Shortfall" v={n(s.shortfall)} x="units allocation may fill" />
          <Tile k="Bin stock" v={n(s.bin_pcs)} x={cover == null ? '—' : `covers ${cov(cover)} of the shortfall`}
            tone={cover != null && cover < 100 ? 'amber' : undefined} />
          <Tile k="Excess" v={n(s.excess)} x={`${n(s.rows_excess)} rows over target`} />
          <Tile k="No store can take" v={n(s.unreachable_pcs)} x={`${pct(s.unreachable_pcs, s.bin_pcs)} of bin stock`}
            tone={s.unreachable_pcs ? 'amber' : undefined} />
        </div>
      )}

      {L && (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.3fr) minmax(0, .7fr)', gap: 10 }}>
          <div style={{ display: 'grid', gap: 10, alignContent: 'start' }}>
            <div className="card">
              <Head title={`Checks · build ${L.BUILD_ID}`} right={L.CHECKS_PASSED ? 'passed — switched in' : 'failed'} />
              <div style={{ padding: 10 }}><CheckList items={L.checks || []} /></div>
            </div>
            <Categories rows={s?.by_category || []} />
          </div>
          <div style={{ display: 'grid', gap: 10, alignContent: 'start' }}>
            <SettingsUsed used={L.settings || {}} now={st.settings || {}} changed={f?.changed_settings || []} />
            <Rules rules={s?.cont_rules || {}} total={s?.rows || 0} />
            <History builds={st.builds || []} />
          </div>
        </div>
      )}

      {/* Readable during a build: rows are staged elsewhere until the switch. */}
      {L && <Browse key={L.BUILD_ID} />}
    </div>
  )
}

function Freshness({ f, latest, onBuild, disabled }) {
  const t = { none: ['blue', 'Not built yet'], stale: ['amber', 'Out of date'], aging: ['blue', 'Store stock has moved'],
              fresh: ['green', 'Up to date'] }[f.state] || ['blue', f.state]
  const tone = { amber: [C.amberBg, C.amberBd, C.amber], blue: [C.blueBg, C.blueBd, C.blue], green: [C.greenBg, C.greenBd, C.green] }[t[0]]
  const lines = f.state === 'none'
    ? ['Build MBQ turns the loaded sheets into a target and a shortfall per store and article. Allocation needs it.']
    : f.state === 'fresh'
      ? [`Build ${latest.BUILD_ID} was made from upload ${latest.UPLOAD_ID} with today's settings${latest.DURATION_SEC ? `, in ${Math.round(latest.DURATION_SEC)} s` : ''}.`]
      : [...(f.reasons || []), ...(f.notes || [])]
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, background: tone[0], border: `1px solid ${tone[1]}`,
                  borderRadius: 8, padding: '8px 11px' }}>
      <span className="badge" style={{ color: '#fff', background: tone[2] }}>{t[1]}</span>
      <div style={{ fontSize: 11, color: C.textSub, lineHeight: 1.45, minWidth: 0 }}>
        {lines.map((l, i) => <div key={i}>{l}</div>)}
      </div>
      {f.state !== 'fresh' && (
        <button type="button" className="btn-primary btn-sm" style={{ marginLeft: 'auto' }} onClick={onBuild} disabled={disabled}>
          <Hammer size={11} /> {f.state === 'none' ? 'Build' : 'Rebuild'}
        </button>
      )}
    </div>
  )
}

function Categories({ rows }) {
  const [q, setQ] = useState('')
  const [all, setAll] = useState(false)
  const shown = rows.filter(r => !q || r.maj_cat.toLowerCase().includes(q.toLowerCase()))
  const list = all ? shown : shown.slice(0, 15)
  return (
    <div className="card">
      <Head title="Demand against bin stock, by category"
        right={<input className="input" placeholder="Find a category" value={q} onChange={e => setQ(e.target.value)}
                      style={{ width: 150, height: 24, fontSize: 10.5 }} />} />
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
          <thead>
            <tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase', textAlign: 'right' }}>
              <th style={{ textAlign: 'left', padding: '5px 10px' }}>Category</th>
              <th style={{ padding: '5px 6px' }}>Bin pcs</th><th style={{ padding: '5px 6px' }}>Shortfall</th>
              <th style={{ padding: '5px 6px' }} title="Bin pieces as a share of the shortfall">Covers</th>
              <th style={{ padding: '5px 6px' }}>Rows short</th><th style={{ padding: '5px 10px' }}>Excess</th>
            </tr>
          </thead>
          <tbody>
            {list.map(r => {
              const c = r.shortfall ? r.bin_pcs / r.shortfall * 100 : null
              return (
                <tr key={r.maj_cat} style={{ borderTop: `1px solid ${C.cardBorder}`, textAlign: 'right' }}>
                  <td style={{ textAlign: 'left', padding: '4px 10px', fontWeight: 600 }}>{r.maj_cat}</td>
                  <td style={{ padding: '4px 6px' }}>{n(r.bin_pcs)}</td>
                  <td style={{ padding: '4px 6px' }}>{n(r.shortfall)}</td>
                  <td style={{ padding: '4px 6px', color: c == null ? C.textMuted : c < 100 ? C.amber : C.green }}>
                    {c == null ? (r.bin_pcs ? 'no demand' : '—') : cov(c)}
                  </td>
                  <td style={{ padding: '4px 6px' }}>{n(r.rows_short)}</td>
                  <td style={{ padding: '4px 10px' }}>{n(r.excess)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {shown.length > 15 && (
        <div style={{ padding: '6px 10px', borderTop: `1px solid ${C.cardBorder}` }}>
          <button type="button" className="btn-secondary btn-sm" onClick={() => setAll(a => !a)}>
            {all ? 'Show the top 15' : `Show all ${shown.length}`}
          </button>
        </div>
      )}
    </div>
  )
}

function SettingsUsed({ used, now, changed }) {
  return (
    <div className="card">
      <Head title="Settings this build used" right={changed.length ? `${changed.length} changed since` : 'unchanged since'} />
      <div style={{ padding: '4px 10px 8px', display: 'grid', gap: 2 }}>
        {Object.keys(LABEL).map(k => {
          const moved = changed.includes(k)
          return (
            <div key={k} style={{ display: 'flex', fontSize: 10.5, gap: 6, padding: '2px 0', color: moved ? C.amber : C.text }}>
              <span style={{ color: moved ? C.amber : C.textSub }} title={k}>{LABEL[k]}</span>
              <span style={{ marginLeft: 'auto', fontWeight: 700 }}>
                {fmtSetting(k, used[k])}{moved && <span style={{ fontWeight: 400 }}> → {fmtSetting(k, now[k])}</span>}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

const RULE_WORDS = {
  'MASTER_NORM · STORE': 'Store size curve (Master_CONT_SZ)', 'MASTER_NORM · CO': 'Company size curve (CO row)',
  'REQ_FB_NORM · REQ': 'REQ mix — the master had none', 'NO_REQ_SZ · (none)': 'Size not asked for → 0',
  'REQ_SHARE · REQ': 'REQ mix', 'FULL_SZ · (none)': 'Forced to 100%', 'FALLBACK · (none)': 'No row → fallback',
}
function Rules({ rules, total }) {
  const entries = Object.entries(rules)
  if (!entries.length) return null
  return (
    <div className="card">
      <Head title="Where each size share came from" />
      <div style={{ padding: '6px 10px 10px', display: 'grid', gap: 6 }}>
        {entries.map(([k, v]) => (
          <div key={k}>
            <div style={{ display: 'flex', fontSize: 10.5 }}>
              <span style={{ color: C.textSub }} title={k}>{RULE_WORDS[k] || k}</span>
              <span style={{ marginLeft: 'auto', fontWeight: 700 }}>{n(v)} <span style={{ color: C.textMuted, fontWeight: 400 }}>{pct(v, total)}</span></span>
            </div>
            <div style={{ height: 5, borderRadius: 3, background: C.grayBg, overflow: 'hidden', marginTop: 2 }}>
              <div style={{ width: `${total ? v / total * 100 : 0}%`, height: '100%', background: C.primary }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function History({ builds }) {
  const tone = { DONE: [C.green, C.greenBg], RUNNING: [C.indigo, C.indigoBg], FAILED: [C.red, C.redBg], CANCELLED: [C.gray, C.grayBg] }
  return (
    <div className="card">
      <Head title="Builds" />
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
        <tbody>
          {builds.map(b => {
            const t = tone[b.STATUS] || [C.gray, C.grayBg]
            return (
              <tr key={b.BUILD_ID} style={{ borderTop: `1px solid ${C.cardBorder}` }} title={b.ERROR || b.PROGRESS || ''}>
                <td style={{ padding: '4px 10px', fontWeight: 700 }}>{b.BUILD_ID}</td>
                <td style={{ padding: '4px 4px' }}><span className="badge" style={{ color: t[0], background: t[1] }}>{b.STATUS}</span></td>
                <td style={{ padding: '4px 4px', textAlign: 'right' }}>{b.ROWS_BUILT ? `${n(b.ROWS_BUILT)} rows` : ''}</td>
                <td style={{ padding: '4px 4px', textAlign: 'right', color: C.textMuted }}>{b.DURATION_SEC ? `${Math.round(b.DURATION_SEC)} s` : ''}</td>
                <td style={{ padding: '4px 10px', textAlign: 'right', color: C.textMuted, whiteSpace: 'nowrap' }}>{when(b.STARTED_AT)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

const COLS = [
  ['STORE_CODE', 'Store'], ['ART', 'Article'], ['MAJ_CAT', 'Category'], ['BIN_SIZE', 'Size'],
  ['CONT_EFF', 'Share', v => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)], ['ACC_D', 'ACC_D'],
  ['NORM_DAYS', 'Days'], ['MBQ_ROUNDED', 'Target'], ['STK_TTL', 'Stock', d2], ['SHORTFALL', 'Short', d2],
  ['EXCESS', 'Excess', d2], ['BIN_QTY', 'In bins'],
]

function Browse() {
  const [flt, setFlt] = useState({ store: '', art: '', maj_cat: '', only: 'short', sort: 'shortfall' })
  const [page, setPage] = useState(1)
  const [res, setRes] = useState(null)
  const [loading, setLoading] = useState(false)
  const [pick, setPick] = useState(null)
  const size = 50

  const fetchRows = useCallback(async (p = 1, f = flt) => {
    setLoading(true)
    try {
      const params = { ...Object.fromEntries(Object.entries(f).filter(([, v]) => v)), page: p, size }
      const { data: r } = await b2bAPI.mbqRows(params)
      setRes(r.data); setPage(p)
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not read the demand table') }
    finally { setLoading(false) }
  }, [flt])

  useEffect(() => { fetchRows(1) }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k, v) => setFlt(o => ({ ...o, [k]: v }))
  const pages = res ? Math.max(1, Math.ceil(res.total / size)) : 1

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 340px', gap: 10, alignItems: 'start' }}>
      <div className="card">
        <Head title="The demand table" right={res ? `${n(res.total)} rows match` : ''} />
        <form onSubmit={e => { e.preventDefault(); fetchRows(1) }}
          style={{ display: 'flex', gap: 6, padding: 8, flexWrap: 'wrap', borderBottom: `1px solid ${C.cardBorder}` }}>
          <input className="input" placeholder="Store" value={flt.store} onChange={e => set('store', e.target.value)} style={{ width: 80 }} />
          <input className="input" placeholder="Article" value={flt.art} onChange={e => set('art', e.target.value)} style={{ width: 130 }} />
          <input className="input" placeholder="Category" value={flt.maj_cat} onChange={e => set('maj_cat', e.target.value)} style={{ width: 130 }} />
          <select className="input" value={flt.only} onChange={e => set('only', e.target.value)} style={{ width: 120 }}>
            <option value="">All rows</option><option value="short">Short only</option>
            <option value="excess">Excess only</option><option value="zero">Target zero</option>
          </select>
          <select className="input" value={flt.sort} onChange={e => set('sort', e.target.value)} style={{ width: 140 }}>
            <option value="shortfall">Biggest shortfall</option><option value="excess">Biggest excess</option>
            <option value="mbq">Biggest target</option><option value="store">By store</option><option value="article">By article</option>
          </select>
          <button type="submit" className="btn-primary btn-sm" disabled={loading}><Search size={11} /> Show</button>
        </form>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
            <thead>
              <tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
                {COLS.map(([k, l]) => <th key={k} style={{ padding: '5px 7px', textAlign: ['STORE_CODE', 'ART', 'MAJ_CAT', 'BIN_SIZE'].includes(k) ? 'left' : 'right', whiteSpace: 'nowrap' }}>{l}</th>)}
              </tr>
            </thead>
            <tbody style={{ opacity: loading ? 0.5 : 1 }}>
              {(res?.rows || []).map(r => {
                const on = pick && pick.store === r.STORE_CODE && pick.art === r.ART
                return (
                  <tr key={`${r.STORE_CODE}-${r.ART}`} onClick={() => setPick({ store: r.STORE_CODE, art: r.ART })}
                    style={{ borderTop: `1px solid ${C.cardBorder}`, cursor: 'pointer', background: on ? C.primaryLt : undefined }}
                    title="Explain this row">
                    {COLS.map(([k, , fmt]) => (
                      <td key={k} style={{ padding: '4px 7px', whiteSpace: 'nowrap',
                                           textAlign: ['STORE_CODE', 'ART', 'MAJ_CAT', 'BIN_SIZE'].includes(k) ? 'left' : 'right',
                                           fontWeight: k === 'SHORTFALL' ? 700 : 400 }}>
                        {fmt ? fmt(r[k]) : (r[k] ?? '—')}
                      </td>
                    ))}
                  </tr>
                )
              })}
              {res && !res.rows.length && <tr><td colSpan={COLS.length} style={{ padding: 12, color: C.textMuted }}>No rows match</td></tr>}
            </tbody>
          </table>
        </div>
        {res && res.total > size && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px', borderTop: `1px solid ${C.cardBorder}`, fontSize: 10.5 }}>
            <button type="button" className="btn-secondary btn-sm" disabled={page <= 1 || loading} onClick={() => fetchRows(page - 1)}><ChevronLeft size={11} /></button>
            <span>Page {n(page)} of {n(pages)}</span>
            <button type="button" className="btn-secondary btn-sm" disabled={page >= pages || loading} onClick={() => fetchRows(page + 1)}><ChevronRight size={11} /></button>
          </div>
        )}
      </div>
      <Explain pick={pick} />
    </div>
  )
}

function Explain({ pick }) {
  const [store, setStore] = useState('')
  const [art, setArt] = useState('')
  const [res, setRes] = useState(null)
  const [loading, setLoading] = useState(false)

  const run = useCallback(async (s, a) => {
    if (!s || !a) return
    setLoading(true)
    try { const { data: r } = await b2bAPI.mbqExplain(s, a); setRes(r.data) }
    catch (e) { toast.error(e.response?.data?.detail || 'Could not explain that row') }
    finally { setLoading(false) }
  }, [])

  useEffect(() => {
    if (pick) { setStore(pick.store); setArt(pick.art); run(pick.store, pick.art) }
  }, [pick, run])

  return (
    <div className="card" style={{ position: 'sticky', top: 8 }}>
      <Head title="Why this number?" />
      <form onSubmit={e => { e.preventDefault(); run(store.trim(), art.trim()) }}
        style={{ display: 'flex', gap: 6, padding: 8, borderBottom: `1px solid ${C.cardBorder}` }}>
        <input className="input" placeholder="Store" value={store} onChange={e => setStore(e.target.value)} style={{ width: 70 }} />
        <input className="input" placeholder="Article" value={art} onChange={e => setArt(e.target.value)} style={{ flex: 1, minWidth: 0 }} />
        <button type="submit" className="btn-primary btn-sm" disabled={loading}>Explain</button>
      </form>
      <div style={{ padding: 10, fontSize: 10.5 }}>
        {!res && <div style={{ color: C.textMuted }}>Click a row, or type a store and an article, to walk it through the build step by step.</div>}
        {res && (
          <div style={{ display: 'grid', gap: 7, opacity: loading ? 0.5 : 1 }}>
            <div style={{ fontSize: 11.5, fontWeight: 700, lineHeight: 1.4 }}>{res.verdict}</div>
            {res.steps.map((s, i) => (
              <div key={i} style={{ display: 'grid', gridTemplateColumns: '14px minmax(0, 1fr)', gap: 6 }}>
                {s.ok ? <Check size={12} style={{ color: C.green, marginTop: 1 }} /> : <XCircle size={12} style={{ color: C.red, marginTop: 1 }} />}
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: 'flex', gap: 6 }}>
                    <span style={{ color: C.textSub }}>{s.label}</span>
                    <b style={{ marginLeft: 'auto', textAlign: 'right' }}>{s.value}</b>
                  </div>
                  {s.detail && <div style={{ color: C.textMuted, fontSize: 10, lineHeight: 1.4, marginTop: 1 }}>{s.detail}</div>}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function Head({ title, right }) {
  return (
    <div style={{ padding: '7px 10px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: C.textSub }}>{title}</span>
      {right && <span style={{ marginLeft: 'auto', fontSize: 10, color: C.textMuted }}>{right}</span>}
    </div>
  )
}

function Tile({ k, v, x, tone }) {
  return (
    <div className="card" style={{ padding: '9px 11px', borderColor: tone === 'amber' ? C.amberBd : undefined }}>
      <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '.07em', textTransform: 'uppercase', color: C.textMuted }}>{k}</div>
      <div style={{ fontSize: 19, fontWeight: 800, marginTop: 2, fontVariantNumeric: 'tabular-nums',
                    color: tone === 'amber' ? C.amber : C.text }}>{v}</div>
      <div style={{ fontSize: 10, color: C.textSub, marginTop: 1 }}>{x}</div>
    </div>
  )
}
