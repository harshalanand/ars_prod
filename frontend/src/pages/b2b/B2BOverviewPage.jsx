import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, Wrench, ArrowRight, Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { b2bAPI } from '@/services/api'
import { C } from '@/theme/colors'
import B2BStepper from '@/components/b2b/B2BStepper'
import CheckList from '@/components/b2b/CheckList'

const n = v => (v == null ? '—' : Math.round(+v).toLocaleString())
const when = s => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
const LINK = { upload: '/bin-alloc/upload', settings: '/bin-alloc/settings', mbq: '/bin-alloc/mbq', run: '/bin-alloc/run' }
const LINK_WORD = { upload: 'Upload Data', settings: 'Settings', mbq: 'Build MBQ', run: 'Run Allocation' }

export default function B2BOverviewPage() {
  const nav = useNavigate()
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const timer = useRef(null)

  const load = useCallback(async (quiet) => {
    if (!quiet) setBusy(true)
    try {
      const { data: r } = await b2bAPI.overview()
      setData(r.data)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Could not load the overview')
    } finally { if (!quiet) setBusy(false) }
  }, [])

  useEffect(() => { load() }, [load])
  // While a load holds the tables, re-read every few seconds until it ends.
  useEffect(() => {
    clearInterval(timer.current)
    if (data?.loading) timer.current = setInterval(() => load(true), 3000)
    return () => clearInterval(timer.current)
  }, [data?.loading, load])

  const repair = async () => {
    try {
      const { data: r } = await b2bAPI.repairTables()
      r.success ? toast.success(r.message) : toast.error(r.message)
      load(true)
    } catch (e) { toast.error(e.response?.data?.detail || 'Repair failed') }
  }

  const t = data?.tiles
  const open = (data?.checks || []).filter(c => c.level === 'warn' || c.level === 'block').length
  const nextStep = (data?.checks || []).find(c => (c.level === 'warn' || c.level === 'block') && c.link)

  return (
    <div className="space-y-3">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title">Bin-to-Bin Transfer</h1>
          <p className="page-subtitle">RDC bin stock → store pick list · GRT ALC</p>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <button type="button" className="btn-secondary" onClick={() => load()} disabled={busy}>
            <RefreshCw size={12} className={busy ? 'animate-spin' : ''} /> Refresh
          </button>
          <button type="button" className="btn-secondary" onClick={repair}
            title="Create any GRT ALC table that is missing. Never deletes anything.">
            <Wrench size={12} /> Repair tables
          </button>
          {nextStep && (
            <button type="button" className="btn-primary" onClick={() => nav(LINK[nextStep.link])}>
              Next: {open} to review <ArrowRight size={12} />
            </button>
          )}
        </div>
      </div>

      <B2BStepper pipeline={data?.pipeline} />

      {data?.loading && (
        <div className="card" style={{ padding: 12, display: 'flex', alignItems: 'center', gap: 10,
                                       borderColor: C.blueBd, background: C.blueBg }}>
          <Loader2 size={16} className="animate-spin" style={{ color: C.blue }} />
          <div style={{ fontSize: 11 }}>
            <b>Upload {data.loading.UPLOAD_ID} is loading</b> — {data.loading.PROGRESS}.
            The tables are locked until it commits, so the figures below will return when it finishes.
          </div>
        </div>
      )}

      {t && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 8 }}>
          <Tile k="Stock in bins" v={n(t.bin.qty)}
            x={Object.entries(t.bin.by_rdc || {}).map(([r, v]) => `${r} ${n(v.qty)}`).join(' · ') || 'nothing loaded'} />
          <Tile k="Store Master" v={n(t.store.rows)}
            x={Object.entries(t.store.by_rdc || {}).map(([r, v]) => `${r} ${n(v)}`).join(' · ') || 'nothing loaded'} />
          <Tile k="Requested" v={n(t.req.units)} x={`${n(t.req.rows)} REQ lines · ${n(t.req.stores)} stores`} />
          <Tile k="Demand rows" v={n(t.mbq.rows)}
            x={data.building ? `build ${data.building.BUILD_ID} running · ${data.building.PROGRESS_PCT || 0}%`
              : t.mbq.build_id ? `build ${t.mbq.build_id} · ${n(t.mbq.shortfall)} units short`
                + (t.mbq.state === 'stale' ? ' · out of date' : '') : 'not built yet'} />
        </div>
      )}

      {data && !data.loading && (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.25fr) minmax(0, .75fr)', gap: 10 }}>
          <div className="card">
            <Head title="Needs attention" right={`${open} open · ${(data.checks || []).filter(c => c.level === 'ok').length} clear`} />
            <div style={{ padding: 10 }}>
              <CheckList items={data.checks} actions={Object.fromEntries(
                (data.checks || []).filter(c => c.link).map(c => [c.code, () => (
                  <button type="button" className="btn-secondary btn-sm" onClick={() => nav(LINK[c.link])}>
                    Open {LINK_WORD[c.link]}
                  </button>
                )]))} />
            </div>
          </div>

          <div style={{ display: 'grid', gap: 10, alignContent: 'start' }}>
            <div className="card">
              <Head title="Recent uploads" right={<button type="button" className="btn-secondary btn-sm"
                onClick={() => nav('/bin-alloc/upload')}>Upload Data</button>} />
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5 }}>
                <tbody>
                  {(data.uploads || []).map(u => (
                    <tr key={u.UPLOAD_ID} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
                      <td style={{ padding: '5px 10px', fontWeight: 700 }}>{u.UPLOAD_ID}</td>
                      <td style={{ padding: '5px 4px' }}><Status s={u.STATUS} /></td>
                      <td style={{ padding: '5px 4px', color: C.textSub, maxWidth: 150, overflow: 'hidden',
                                   textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                        title={u.SOURCE_NAME}>{String(u.SOURCE_NAME || '').split(/[\\/]/).pop()}</td>
                      <td style={{ padding: '5px 10px', color: C.textMuted, textAlign: 'right', whiteSpace: 'nowrap' }}>
                        {when(u.LOADED_AT || u.CHECKED_AT || u.CREATED_AT)}
                      </td>
                    </tr>
                  ))}
                  {!(data.uploads || []).length && (
                    <tr><td style={{ padding: 10, color: C.textMuted }}>No uploads yet</td></tr>
                  )}
                </tbody>
              </table>
            </div>

            {data.legacy && (
              <div className="card">
                <Head title="Side by side with the Streamlit tool" />
                <div style={{ padding: '4px 10px 10px', fontSize: 10.5, color: C.textSub }}>
                  <p style={{ margin: '4px 0 6px' }}>
                    The tool keeps its own <code>B2B_*</code> tables. Both can run on the same workbook; matching
                    results is how this module is proven before the tool is retired.
                  </p>
                  <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                    <thead>
                      <tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
                        <th style={{ textAlign: 'left', fontWeight: 700 }}></th>
                        <th style={{ textAlign: 'right', fontWeight: 700 }}>This module</th>
                        <th style={{ textAlign: 'right', fontWeight: 700 }}>Streamlit</th>
                      </tr>
                    </thead>
                    <tbody style={{ fontVariantNumeric: 'tabular-nums' }}>
                      <Cmp k="Bin rows" a={t?.bin.rows} b={data.legacy.bin?.rows} />
                      <Cmp k="Bin pieces" a={t?.bin.qty} b={data.legacy.bin?.total} />
                      <Cmp k="Stores" a={t?.store.rows} b={data.legacy.store?.rows} />
                      <Cmp k="REQ lines" a={t?.req.rows} b={data.legacy.req?.rows} />
                      <Cmp k="REQ units" a={t?.req.units} b={data.legacy.req?.total} />
                    </tbody>
                  </table>
                  {data.legacy.session && (
                    <p style={{ margin: '6px 0 0' }}>
                      Its latest session: <b>{data.legacy.session.id}</b> · {when(data.legacy.session.at)} ·{' '}
                      {n(data.legacy.session.units)} units · {data.legacy.session.fill} · warehouse {data.legacy.session.cross}
                    </p>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function Head({ title, right }) {
  return (
    <div style={{ padding: '7px 10px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex',
                  alignItems: 'center', gap: 8 }}>
      <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase',
                     color: C.textSub }}>{title}</span>
      {right && <span style={{ marginLeft: 'auto', fontSize: 10, color: C.textMuted }}>{right}</span>}
    </div>
  )
}

function Tile({ k, v, x }) {
  return (
    <div className="card" style={{ padding: '9px 11px' }}>
      <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '.07em', textTransform: 'uppercase',
                    color: C.textMuted }}>{k}</div>
      <div style={{ fontSize: 19, fontWeight: 800, marginTop: 2, fontVariantNumeric: 'tabular-nums' }}>{v}</div>
      <div style={{ fontSize: 10, color: C.textSub, marginTop: 1 }}>{x}</div>
    </div>
  )
}

function Cmp({ k, a, b }) {
  const same = a != null && b != null && Math.round(+a) === Math.round(+b)
  return (
    <tr style={{ borderTop: `1px solid ${C.cardBorder}` }}>
      <td style={{ padding: '3px 0' }}>{k}</td>
      <td style={{ padding: '3px 0', textAlign: 'right', fontWeight: 700 }}>{n(a)}</td>
      <td style={{ padding: '3px 0', textAlign: 'right', color: same ? C.green : C.textSub }}>{n(b)}</td>
    </tr>
  )
}

export function Status({ s }) {
  const tone = {
    LOADED: [C.green, C.greenBg], CHECKED: [C.blue, C.blueBg], CHECKING: [C.indigo, C.indigoBg],
    LOADING: [C.indigo, C.indigoBg], QUEUED: [C.gray, C.grayBg], FAILED: [C.red, C.redBg],
    CANCELLED: [C.gray, C.grayBg], EXPIRED: [C.amber, C.amberBg],
  }[s] || [C.gray, C.grayBg]
  return <span className="badge" style={{ color: tone[0], background: tone[1] }}>{s}</span>
}
