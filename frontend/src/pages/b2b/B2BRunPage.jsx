import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Play, FlaskConical, X, RefreshCw, Trash2, Hammer, AlertTriangle } from 'lucide-react'
import toast from 'react-hot-toast'
import { b2bAPI } from '@/services/api'
import { C } from '@/theme/colors'
import B2BStepper from '@/components/b2b/B2BStepper'
import CheckList from '@/components/b2b/CheckList'

const n = v => (v == null ? '—' : Math.round(+v).toLocaleString())
const when = s => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
const pct = (a, b) => (b ? `${((a / b) * 100).toFixed(1)}%` : '—')

// The four ways stock can be shared: fill mode × fair basis, in plain words.
const SHARING = [
  { key: 'GREEDY', fill: 'GREEDY', fair: 'PROPORTIONAL', name: 'In priority order',
    help: 'The first store in the queue takes everything it can, then the next. Simple — but when many stores tie, the alphabet decides who gets a scarce article (the tool measured 7 of 161 stores taking all 890 units).' },
  { key: 'RR_PROPORTIONAL', fill: 'ROUND_ROBIN', fair: 'PROPORTIONAL', name: 'Share — furthest from its own REQ first',
    help: 'One unit at a time per store, cycling. Between articles the queue is re-ordered so the store furthest from its own requirement goes first. A big store still gets more than a small one; nobody sweeps an article.' },
  { key: 'RR_FLAT', fill: 'ROUND_ROBIN', fair: 'FLAT', name: 'Share — fewest units so far first',
    help: 'One unit at a time; whoever has received the fewest units goes first, whatever it asked for. Gives a small store the same as a large one.' },
  { key: 'RR_NONE', fill: 'ROUND_ROBIN', fair: 'NONE', name: 'Share — priority order, no re-order',
    help: 'One unit at a time, but every article restarts the queue in the same order. Reproduces the tool’s original round robin.' },
]
const WAREHOUSE = [
  { key: 'SAME', name: 'Own RDC only', help: 'A store gets stock only from its own warehouse. The same article in two warehouses is two separate pools.' },
  { key: 'HOME_FIRST', name: 'Own RDC first', help: 'The store’s own warehouse is drawn first; the other is used only when that runs out.' },
  { key: 'ANY', name: 'All RDCs', help: 'One pool per article. Stock may go from either warehouse to any store.' },
]
const PRIORITY = { SHORTFALL_DESC: 'Biggest shortfall', CONT_DESC: 'Highest size share', MBQ_DESC: 'Biggest target' }
const PICK = { MAX_CONSUMPTION: 'Fewest picks', BIN_ORDER: 'Bin order' }
const WH_WORD = Object.fromEntries(WAREHOUSE.map(w => [w.key, w.name]))
const shareName = (f, b) => (SHARING.find(s => s.fill === f && (f === 'GREEDY' || s.fair === b)) || {}).name || f

export default function B2BRunPage() {
  const nav = useNavigate()
  const [page, setPage] = useState(null)
  const [form, setForm] = useState(null)
  const [job, setJob] = useState(null)
  const [shown, setShown] = useState(null)          // a finished session to show
  const [busy, setBusy] = useState(false)
  const poll = useRef(null)

  const load = useCallback(async () => {
    try {
      const { data: r } = await b2bAPI.runPage()
      setPage(r.data)
      setForm(f => f || { ...r.data.defaults, sharing: r.data.defaults.fill_mode === 'GREEDY' ? 'GREEDY' : `RR_${r.data.defaults.fair_basis}`,
                          cross_rdc: '', dry_run: false, note: '' })
      return r.data
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not load the run page') }
  }, [])

  const show = useCallback(async (id) => {
    try { const { data: r } = await b2bAPI.runSession(id); setShown(r.data) } catch { /* ignore */ }
  }, [])

  const watch = useCallback((id) => {
    clearInterval(poll.current)
    const tick = async () => {
      try {
        const { data: r } = await b2bAPI.runSession(id)
        if (r.data.STATUS === 'RUNNING') { setJob(r.data); return }
        clearInterval(poll.current)
        setJob(null); setShown(r.data)
        const msg = r.data.progress?.text || r.data.STATUS
        r.data.STATUS === 'DONE' ? (r.data.CHECKS_PASSED ? toast.success(msg) : toast.error(`${msg} — checks failed`))
          : r.data.STATUS === 'CANCELLED' ? toast(msg) : toast.error(msg)
        load()
      } catch { clearInterval(poll.current) }
    }
    tick()
    poll.current = setInterval(tick, 1500)
  }, [load])

  useEffect(() => {
    load().then(d => {
      if (d?.running) watch(d.running.SESSION_ID)
      else if (d?.sessions?.length) show(d.sessions[0].SESSION_ID)
    })
    return () => clearInterval(poll.current)
  }, [load, watch, show])

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const sharing = SHARING.find(s => s.key === form?.sharing) || SHARING[0]
  const canRun = page?.gates?.ok && form?.cross_rdc && !job && !busy

  const run = async () => {
    setBusy(true)
    try {
      const body = { priority: form.priority, min_qty: +form.min_qty, fill_mode: sharing.fill, fair_basis: sharing.fair,
                     bin_pick: form.bin_pick, cross_rdc: form.cross_rdc, dry_run: form.dry_run, note: form.note || null }
      const { data: r } = await b2bAPI.startRun(body)
      toast(r.message)
      watch(r.data.session_id)
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not start the run') }
    finally { setBusy(false) }
  }
  const cancel = async () => { if (job) { try { const { data: r } = await b2bAPI.cancelRun(job.SESSION_ID); toast(r.message) } catch { /* ignore */ } } }
  const remove = async (s) => {
    const typed = window.prompt(`Delete session ${s.SESSION_ID} (${n(s.UNITS_ALLOCATED)} units) from all three tables?\nType DELETE to confirm.`)
    if (typed !== 'DELETE') return
    try {
      const { data: r } = await b2bAPI.deleteSession(s.SESSION_ID)
      toast.success(r.message)
      if (shown?.SESSION_ID === s.SESSION_ID) setShown(null)
      load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not delete') }
  }

  const wh = page?.warehouses
  return (
    <div className="space-y-3">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title">4 · Run Allocation</h1>
          <p className="page-subtitle">Hand each article’s bin stock to the stores that are short of it. Every run is a new session — nothing is overwritten.</p>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <button type="button" className="btn-secondary" onClick={load}><RefreshCw size={12} /> Refresh</button>
        </div>
      </div>
      <B2BStepper current="run" />

      {page && <Gates page={page} onBuild={() => nav('/bin-alloc/mbq')} />}

      {form && page && (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 10, alignItems: 'start' }}>
          <Card title="1 · How stock is shared">
            {SHARING.map(s => (
              <Choice key={s.key} on={form.sharing === s.key} onClick={() => set('sharing', s.key)} name={s.name} help={s.help} />
            ))}
          </Card>

          <div style={{ display: 'grid', gap: 10 }}>
            <Card title="2 · Which warehouse may ship" right={!form.cross_rdc && <span style={{ color: C.red, fontWeight: 700 }}>required — choose one</span>}
              tone={!form.cross_rdc ? 'red' : undefined}>
              {WAREHOUSE.map(w => (
                <Choice key={w.key} on={form.cross_rdc === w.key} onClick={() => set('cross_rdc', w.key)} name={w.name} help={w.help}
                  note={w.key !== 'ANY' && wh && Object.keys(wh.stores_without_bins || {}).length > 0
                    ? `${Object.entries(wh.stores_without_bins).map(([r, c]) => `${c} ${r} store(s)`).join(', ')} have no bins in their own warehouse — `
                      + (w.key === 'SAME' ? 'they get nothing.' : 'they are served from the other warehouse.') : null} />
              ))}
              {wh && (
                <div style={{ fontSize: 10.5, color: C.textSub, padding: '6px 2px 0' }}>
                  Bins: {Object.entries(wh.bins).map(([r, v]) => `${r} ${n(v.pcs)} pcs`).join(' · ') || 'none'} &nbsp;|&nbsp;
                  Stores: {Object.entries(wh.stores).map(([r, c]) => `${r} ${c}`).join(' · ')}
                </div>
              )}
            </Card>

            <Card title="3 · Who is served first">
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 140px', gap: 8, alignItems: 'end' }}>
                <label style={{ fontSize: 10.5, color: C.textSub }}>Priority
                  <select className="input" value={form.priority} onChange={e => set('priority', e.target.value)}>
                    {Object.entries(PRIORITY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                  </select>
                </label>
                <label style={{ fontSize: 10.5, color: C.textSub }}>Smallest line
                  <input className="input" type="number" min={1} step={1} value={form.min_qty} onChange={e => set('min_qty', e.target.value)} />
                </label>
              </div>
              <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4 }}>
                Ties fall to the next signal, then store code. When sharing, the smallest line is also the step each store takes per pass.
              </div>
            </Card>

            <Card title="4 · Which bin each unit comes from">
              <div style={{ display: 'flex', gap: 6 }}>
                {Object.entries(PICK).map(([k, v]) => (
                  <button key={k} type="button" onClick={() => set('bin_pick', k)} className={form.bin_pick === k ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}>{v}</button>
                ))}
              </div>
              <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4 }}>
                {form.bin_pick === 'MAX_CONSUMPTION'
                  ? 'One bin that covers the whole line if there is one (the smallest such), otherwise the largest bins first. Changes only the pick list, never what a store gets.'
                  : 'Bins in code order, taking what each holds. The tool’s original walk.'}
              </div>
            </Card>
          </div>
        </div>
      )}

      {form && page && (
        <div className="card" style={{ padding: 10, display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <input className="input" placeholder="Note for this session (optional)" value={form.note} maxLength={200}
            onChange={e => set('note', e.target.value)} style={{ flex: 1, minWidth: 200 }} />
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}
            title="Compute everything and run the checks, but write no lines or picks">
            <input type="checkbox" checked={form.dry_run} onChange={e => set('dry_run', e.target.checked)} /> Dry run
          </label>
          <button type="button" className="btn-primary" onClick={run} disabled={!canRun}
            title={!form.cross_rdc ? 'Choose which warehouse may ship first' : !page.gates.ok ? page.gates.block.join(' ') : ''}>
            {form.dry_run ? <FlaskConical size={12} /> : <Play size={12} />} {form.dry_run ? 'Dry run' : 'Run allocation'}
          </button>
          <span style={{ fontSize: 10.5, color: C.textMuted }}>
            {sharing.name} · {form.cross_rdc ? WH_WORD[form.cross_rdc] : 'warehouse not chosen'} · {PRIORITY[form.priority]} · min {form.min_qty} · {PICK[form.bin_pick]}
          </span>
        </div>
      )}

      {job && <Running job={job} onCancel={cancel} />}
      {shown && !job && <Result s={shown} />}
      {page && <Sessions rows={page.sessions || []} onShow={show} onDelete={remove} current={shown?.SESSION_ID} />}
    </div>
  )
}

function Gates({ page, onBuild }) {
  const b = page.build
  const s = b?.ROWS_BUILT
  return (
    <div style={{ display: 'grid', gap: 6 }}>
      {page.gates.block.map((m, i) => (
        <Banner key={`b${i}`} tone="red" label="Cannot run" text={m}
          action={/MBQ|demand/i.test(m) && <button type="button" className="btn-primary btn-sm" onClick={onBuild}><Hammer size={11} /> Build MBQ</button>} />
      ))}
      {page.gates.warn.map((m, i) => <Banner key={`w${i}`} tone="amber" label="Note" text={m} />)}
      {b && page.gates.ok && (
        <Banner tone="green" label="Demand"
          text={`Build ${b.BUILD_ID} from upload ${b.UPLOAD_ID} · ${n(s)} store × article rows · ${n(b.SHORTFALL_TOTAL)} units short.`} />
      )}
    </div>
  )
}

function Running({ job, onCancel }) {
  const p = job.progress || {}
  return (
    <div className="card" style={{ padding: 12, display: 'grid', gap: 8 }}>
      <div style={{ display: 'flex', fontSize: 11, gap: 8 }}>
        <b>{job.DRY_RUN ? 'Dry run' : 'Session'} {job.SESSION_ID}</b>
        <span style={{ color: C.textSub }}>{p.text || 'Starting'}</span>
        <span style={{ marginLeft: 'auto', color: C.textMuted }}>{job.PROGRESS_PCT || 0}%</span>
      </div>
      <div style={{ height: 7, borderRadius: 4, background: C.grayBd, overflow: 'hidden' }}>
        <div style={{ width: `${job.PROGRESS_PCT || 0}%`, height: '100%', background: C.primary, transition: 'width .4s' }} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 8 }}>
        <Mini k="Lines" v={n(p.lines)} />
        <Mini k="Units allocated" v={n(p.units)} />
        <Mini k="Cross-warehouse" v={n(p.cross)} />
        <Mini k="Candidates scanned" v={p.candidates_total ? `${n(p.candidates)} of ${n(p.candidates_total)}` : n(p.candidates)} />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button type="button" className="btn-secondary btn-sm" onClick={onCancel}><X size={11} /> Cancel</button>
        <span style={{ fontSize: 10.5, color: C.textSub }}>
          Categories run side by side on several workers. Nothing is written until every one has finished and the checks have passed.
        </span>
      </div>
    </div>
  )
}

const SKIP_WORDS = {
  blocked_art_empty: 'Article had run out by the store’s turn', blocked_req_full: 'Store’s REQ for the category + size already filled',
  blocked_no_req: 'No REQ line for the category + size', below_min_qty: 'Would be below the smallest line',
  blocked_no_rdc: 'Article not in a warehouse the store may use',
}
const LEFT_WORDS = {
  NO_SEASON_REQ: 'Every store asked for zero of it', NOT_IN_REQ: 'Category not in REQ (bags, hangers …)',
  NO_STORE_MASTER: 'Wanted only by stores missing from Store Master', NO_STORE_NEED: 'Wanted, but no store asked enough',
  NO_SHORTFALL: 'No store below its target', REQ_CAP_FULL: 'Stores were short but their REQ was already filled',
  PARTIAL: 'Part of the bin moved; demand ran out first',
}

function Result({ s }) {
  const sm = s.summary || {}
  if (s.STATUS !== 'DONE') {
    return (
      <Banner tone={s.STATUS === 'CANCELLED' ? 'amber' : 'red'} label={`Session ${s.SESSION_ID} · ${s.STATUS}`}
        text={`${s.progress?.text || ''} ${String(s.ERROR || '').split('\n')[0]}`} />
    )
  }
  const skips = sm.skips || {}
  const left = sm.leftovers || {}
  return (
    <div className="card">
      <Head title={`${s.DRY_RUN ? 'Dry run' : 'Session'} ${s.SESSION_ID} · ${when(s.CREATED_AT)} · ${s.CREATED_BY || ''}`}
        right={`${shareName(s.ALLOC_FILL_MODE, s.ALLOC_FAIR_BASIS)} · ${WH_WORD[s.ALLOC_CROSS_RDC] || s.ALLOC_CROSS_RDC} · ${PRIORITY[s.ALLOC_PRIORITY] || s.ALLOC_PRIORITY} · min ${s.ALLOC_MIN_QTY} · ${PICK[s.ALLOC_BIN_PICK] || s.ALLOC_BIN_PICK} · build ${s.BUILD_ID} · ${Math.round(s.DURATION_SEC || 0)} s`} />
      <div style={{ padding: 10, display: 'grid', gap: 10 }}>
        {s.DRY_RUN ? (
          <div style={{ fontSize: 10.5, color: C.textSub }}>Dry run: computed and checked, nothing written. Run it for real to get a pick list.</div>
        ) : (
          <div><a href={`/bin-alloc/sessions?id=${s.SESSION_ID}`} className="btn-primary btn-sm" style={{ textDecoration: 'none' }}>
            Open in Sessions &amp; Pick List →</a></div>
        )}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, minmax(0, 1fr))', gap: 8 }}>
          <Mini k="Bin stock" v={n(s.SUPPLY_UNITS)} />
          <Mini k="Allocated" v={n(s.UNITS_ALLOCATED)} x={pct(s.UNITS_ALLOCATED, s.SUPPLY_UNITS)} />
          <Mini k="Left in bins" v={n(s.UNITS_LEFT)} />
          <Mini k="Lines · picks" v={`${n(s.ART_LINES)} · ${n(s.BIN_LINES)}`} />
          <Mini k="Stores served" v={n(s.STORES_SERVED)} />
          <Mini k="Cross-warehouse" v={n(s.CROSS_RDC_UNITS)} x={pct(s.CROSS_RDC_UNITS, s.UNITS_ALLOCATED)}
            tone={s.CROSS_RDC_UNITS > 0 ? 'amber' : undefined} />
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 10 }}>
          <div>
            <Sub>Balance checks {s.CHECKS_PASSED ? '— all pass' : '— FAILED'}</Sub>
            <CheckList items={s.checks || []} />
          </div>
          <div style={{ display: 'grid', gap: 10, alignContent: 'start' }}>
            <div>
              <Sub>Why candidates were skipped (lines, not units)</Sub>
              <Table rows={Object.entries(skips).filter(([, v]) => v).map(([k, v]) => [SKIP_WORDS[k] || k, n(v)])} />
            </div>
            {!s.DRY_RUN && (
              <div>
                <Sub>Stock left in bins, by reason</Sub>
                <Table rows={Object.entries(left).sort((a, b) => b[1].pcs - a[1].pcs)
                  .map(([k, v]) => [<span key={k} title={k}>{LEFT_WORDS[k] || k}</span>, `${n(v.pcs)} pcs`, `${n(v.rows)} bins`])} />
              </div>
            )}
          </div>
        </div>
        {(sm.top_categories || []).length > 0 && (
          <div>
            <Sub>Categories that received the most</Sub>
            <Table head={['Category', 'Units', 'Lines', 'Candidates']}
              rows={sm.top_categories.slice(0, 10).map(c => [c.cat, n(c.units), n(c.lines), n(c.candidates)])} />
          </div>
        )}
      </div>
    </div>
  )
}

function Sessions({ rows, onShow, onDelete, current }) {
  return (
    <div className="card">
      <Head title="Sessions" right="click a row to see it · nothing is ever overwritten" />
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
          <thead>
            <tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase', textAlign: 'left' }}>
              {['#', 'When', 'Kind', 'Sharing', 'Warehouse', 'Units', 'Lines', 'Stores', 'Cross', 'Checks', 'By', ''].map(h =>
                <th key={h} style={{ padding: '5px 8px', whiteSpace: 'nowrap' }}>{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map(s => (
              <tr key={s.SESSION_ID} onClick={() => onShow(s.SESSION_ID)}
                style={{ borderTop: `1px solid ${C.cardBorder}`, cursor: 'pointer', background: current === s.SESSION_ID ? C.primaryLt : undefined }}>
                <td style={{ padding: '4px 8px', fontWeight: 700 }}>{s.SESSION_ID}</td>
                <td style={{ padding: '4px 8px', whiteSpace: 'nowrap' }}>{when(s.CREATED_AT)}</td>
                <td style={{ padding: '4px 8px' }}><Badge s={s.STATUS} dry={s.DRY_RUN} /></td>
                <td style={{ padding: '4px 8px' }}>{shareName(s.ALLOC_FILL_MODE, s.ALLOC_FAIR_BASIS)}</td>
                <td style={{ padding: '4px 8px' }}>{WH_WORD[s.ALLOC_CROSS_RDC] || s.ALLOC_CROSS_RDC}</td>
                <td style={{ padding: '4px 8px', textAlign: 'right' }}>{n(s.UNITS_ALLOCATED)}</td>
                <td style={{ padding: '4px 8px', textAlign: 'right' }}>{n(s.ART_LINES)}</td>
                <td style={{ padding: '4px 8px', textAlign: 'right' }}>{n(s.STORES_SERVED)}</td>
                <td style={{ padding: '4px 8px', textAlign: 'right' }}>{pct(s.CROSS_RDC_UNITS, s.UNITS_ALLOCATED)}</td>
                <td style={{ padding: '4px 8px' }}>{s.CHECKS_PASSED == null ? '—' : s.CHECKS_PASSED
                  ? <span style={{ color: C.green, fontWeight: 700 }}>pass</span> : <span style={{ color: C.red, fontWeight: 700 }}>FAIL</span>}</td>
                <td style={{ padding: '4px 8px', color: C.textSub }}>{s.CREATED_BY}</td>
                <td style={{ padding: '4px 8px' }} onClick={e => e.stopPropagation()}>
                  {s.STATUS !== 'RUNNING' && (
                    <button type="button" className="btn-secondary btn-sm" onClick={() => onDelete(s)} title="Delete this session"><Trash2 size={11} /></button>
                  )}
                </td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={12} style={{ padding: 12, color: C.textMuted }}>No sessions yet</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Badge({ s, dry }) {
  const t = { DONE: [C.green, C.greenBg], RUNNING: [C.indigo, C.indigoBg], FAILED: [C.red, C.redBg], CANCELLED: [C.gray, C.grayBg] }[s] || [C.gray, C.grayBg]
  return (
    <span style={{ display: 'inline-flex', gap: 4 }}>
      <span className="badge" style={{ color: t[0], background: t[1] }}>{s}</span>
      {dry ? <span className="badge" style={{ color: C.blue, background: C.blueBg }}>dry run</span> : null}
    </span>
  )
}

function Choice({ on, onClick, name, help, note }) {
  return (
    <button type="button" onClick={onClick}
      style={{ display: 'grid', gridTemplateColumns: '14px minmax(0, 1fr)', gap: 8, width: '100%', textAlign: 'left', cursor: 'pointer',
               padding: '7px 9px', borderRadius: 6, marginBottom: 5, background: on ? C.primaryLt : C.card,
               border: `1px solid ${on ? C.primary : C.cardBorder}` }} aria-pressed={on}>
      <span style={{ width: 12, height: 12, borderRadius: '50%', marginTop: 2, border: `2px solid ${on ? C.primary : C.grayBd}`,
                     background: on ? C.primary : 'transparent' }} />
      <span style={{ minWidth: 0 }}>
        <span style={{ display: 'block', fontSize: 11, fontWeight: 700, color: C.text }}>{name}</span>
        <span style={{ display: 'block', fontSize: 10.5, color: C.textSub, lineHeight: 1.45, marginTop: 1 }}>{help}</span>
        {note && <span style={{ display: 'flex', gap: 4, fontSize: 10, color: C.amber, marginTop: 3 }}><AlertTriangle size={11} />{note}</span>}
      </span>
    </button>
  )
}

function Card({ title, right, tone, children }) {
  return (
    <div className="card" style={{ borderColor: tone === 'red' ? C.redBd : undefined }}>
      <Head title={title} right={right} />
      <div style={{ padding: 10 }}>{children}</div>
    </div>
  )
}

function Banner({ tone, label, text, action }) {
  const t = { red: [C.redBg, C.redBd, C.red], amber: [C.amberBg, C.amberBd, C.amber], green: [C.greenBg, C.greenBd, C.green] }[tone]
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, background: t[0], border: `1px solid ${t[1]}`, borderRadius: 8, padding: '7px 11px' }}>
      <span className="badge" style={{ color: '#fff', background: t[2] }}>{label}</span>
      <span style={{ fontSize: 11, color: C.textSub, lineHeight: 1.45 }}>{text}</span>
      {action && <span style={{ marginLeft: 'auto' }}>{action}</span>}
    </div>
  )
}

function Head({ title, right }) {
  return (
    <div style={{ padding: '7px 10px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', alignItems: 'center', gap: 8 }}>
      <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: C.textSub }}>{title}</span>
      {right && <span style={{ marginLeft: 'auto', fontSize: 10, color: C.textMuted, textAlign: 'right' }}>{right}</span>}
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

function Table({ head, rows }) {
  if (!rows.length) return <div style={{ fontSize: 10.5, color: C.textMuted }}>None</div>
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
      {head && <thead><tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
        {head.map((h, i) => <th key={i} style={{ padding: '3px 6px', textAlign: i ? 'right' : 'left' }}>{h}</th>)}</tr></thead>}
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
            {r.map((c, j) => <td key={j} style={{ padding: '3px 6px', textAlign: j ? 'right' : 'left', color: j ? C.text : C.textSub }}>{c}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
