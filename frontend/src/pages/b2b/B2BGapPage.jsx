import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Download, AlertTriangle } from 'lucide-react'
import toast from 'react-hot-toast'
import { b2bAPI, saveBlob } from '@/services/api'
import { C } from '@/theme/colors'

const n = v => (v == null || v === '' ? '—' : typeof v === 'number' ? Math.round(v).toLocaleString() : v)
const when = s => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
const WH = { SAME: 'Own RDC only', HOME_FIRST: 'Own RDC first', ANY: 'All RDCs' }
const VERDICT = {
  'A. STOCK, NO REQ': [C.gray, C.grayBg, 'Stock nobody asked for'],
  'B. REQ ONLY FROM ORPHAN STORES': [C.red, C.redBg, 'Only stores missing from Store Master want it'],
  'C. REQ, NO STOCK': [C.red, C.redBg, 'Stores want it, bins hold none'],
  'D. REQ, NOT ENOUGH STOCK': [C.amber, C.amberBg, 'Not enough to go round'],
  'E. STOCK ABOVE REQ': [C.blue, C.blueBg, 'More than the stores can take'],
  'F. BALANCED': [C.green, C.greenBg, 'Stock and REQ match'],
}

export default function B2BGapPage() {
  const [sessions, setSessions] = useState([])
  const [sid, setSid] = useState(undefined)          // undefined = not chosen yet, '' = none
  const [rep, setRep] = useState(null)
  const [busy, setBusy] = useState(false)
  const [sheet, setSheet] = useState('02_Coverage_Matrix')

  useEffect(() => {
    b2bAPI.sessions(60).then(({ data: r }) => {
      const real = r.data.sessions.filter(s => s.STATUS === 'DONE' && !s.DRY_RUN)
      setSessions(real)
      setSid(real[0]?.SESSION_ID ? String(real[0].SESSION_ID) : '')
    }).catch(() => setSid(''))
  }, [])

  const load = useCallback(async (s) => {
    setBusy(true)
    try { const { data: r } = await b2bAPI.gap(s ? Number(s) : null); setRep(r.data) }
    catch (e) { toast.error(e.response?.data?.detail || 'Could not build the report') }
    finally { setBusy(false) }
  }, [])
  useEffect(() => { if (sid !== undefined) load(sid) }, [sid, load])

  const dl = async (what) => {
    const t = toast.loading('Preparing…')
    try {
      const res = what === 'xlsx' ? await b2bAPI.gapWorkbook(sid ? Number(sid) : null) : await b2bAPI.gapSheetCsv(sid ? Number(sid) : null, what)
      toast.success(`Downloaded ${saveBlob(res, 'gap_report')}`, { id: t })
    } catch { toast.error('Download failed', { id: t }) }
  }

  const sum = rep?.previews?.['01_Summary']?.rows || []
  const verdicts = sum.filter(r => r[0] === 'Gap verdict')
  const fact = label => (sum.find(r => r[1] === label) || [])[2]
  const wh = rep?.previews?.['13_Warehouse_Balance']
  const missing = rep?.previews?.['07_Stores_Missing_Master']
  const mismatch = rep?.previews?.['11_Key_Mismatch']
  const p = rep?.previews?.[sheet]
  const s = sessions.find(x => String(x.SESSION_ID) === String(sid))

  return (
    <div className="space-y-3">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title">Gap Report</h1>
          <p className="page-subtitle">Where bin stock and store requirement do not line up. Read only — nothing is changed.</p>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, alignItems: 'center' }}>
          <select className="input" value={sid ?? ''} onChange={e => setSid(e.target.value)} style={{ width: 340 }}>
            <option value="">No session — stock against requirement only</option>
            {sessions.map(x => <option key={x.SESSION_ID} value={x.SESSION_ID}>Session {x.SESSION_ID} · {when(x.CREATED_AT)} · {WH[x.ALLOC_CROSS_RDC] || x.ALLOC_CROSS_RDC} · {n(x.UNITS_ALLOCATED)} units</option>)}
          </select>
          <button type="button" className="btn-secondary" onClick={() => load(sid)} disabled={busy}><RefreshCw size={12} className={busy ? 'animate-spin' : ''} /> Refresh</button>
          <button type="button" className="btn-primary" onClick={() => dl('xlsx')} disabled={!rep}><Download size={12} /> Whole report (Excel)</button>
        </div>
      </div>

      {busy && !rep && <div className="card" style={{ padding: 12, fontSize: 11, color: C.textSub }}>Building the report… (a few seconds)</div>}
      {rep && (
        <>
          <div style={{ fontSize: 10.5, color: C.textMuted }}>
            Loaded workbook: upload {rep.upload_id} · {s ? `session ${s.SESSION_ID} (${WH[s.ALLOC_CROSS_RDC] || s.ALLOC_CROSS_RDC})` : 'no session'} · built {when(rep.built_at)} in {rep.seconds} s.
            Category + size is matched exactly as the demand build and the REQ cap match it, so every gap here is one allocation meets.
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 8 }}>
            <Tile k="Bin stock" v={n(fact('Bin Master units on hand'))} x="units on hand" />
            <Tile k="Servable REQ" v={n(fact('REQ units from stores in Store Master'))} x="from stores the build can see" />
            <Tile k="REQ that can never be filled" v={n(fact('REQ units from stores NOT in Store Master'))}
              x={`${n(missing?.total)} store(s) missing from Store Master`} tone={missing?.total ? 'red' : null} />
            <Tile k="Join mismatches" v={n(mismatch?.total)} x="category or size in one sheet only" tone={mismatch?.total ? 'amber' : null} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.2fr) minmax(0, .8fr)', gap: 10, alignItems: 'start' }}>
            <div className="card" style={{ padding: 10 }}>
              <Sub>Every category + size, by verdict</Sub>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 6 }}>
                {verdicts.map(v => {
                  const t = VERDICT[v[1]] || [C.gray, C.grayBg, '']
                  return (
                    <div key={v[1]} style={{ background: t[1], borderRadius: 6, padding: '7px 9px' }}>
                      <div style={{ fontSize: 9.5, fontWeight: 800, color: t[0] }}>{v[1]}</div>
                      <div style={{ fontSize: 16, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>{n(v[2])}</div>
                      <div style={{ fontSize: 10, color: C.textSub }}>{t[2]}</div>
                      <div style={{ fontSize: 9.5, color: C.textMuted }}>{v[3]}</div>
                    </div>
                  )
                })}
              </div>
            </div>
            {wh && (
              <div className="card" style={{ padding: 10 }}>
                <Sub>Warehouse balance</Sub>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
                  <thead><tr style={{ color: C.textMuted, fontSize: 9, textTransform: 'uppercase' }}>
                    {['RDC', 'Bin stock', 'Stores', 'Servable REQ', 'Got from own', 'Got from other'].map((h, i) => <th key={h} style={{ textAlign: i ? 'right' : 'left', padding: '3px 4px' }}>{h}</th>)}
                  </tr></thead>
                  <tbody>{wh.rows.map(r => (
                    <tr key={r[0]} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
                      <td style={{ padding: '4px', fontWeight: 700 }}>{r[0]}</td>
                      <td style={{ padding: '4px', textAlign: 'right', color: r[1] ? C.text : C.red }}>{n(r[1])}</td>
                      <td style={{ padding: '4px', textAlign: 'right' }}>{n(r[2])}</td>
                      <td style={{ padding: '4px', textAlign: 'right' }}>{n(r[3])}</td>
                      <td style={{ padding: '4px', textAlign: 'right' }}>{n(r[4])}</td>
                      <td style={{ padding: '4px', textAlign: 'right', color: r[5] ? C.amber : C.text }}>{n(r[5])}</td>
                    </tr>
                  ))}</tbody>
                </table>
                {wh.rows.some(r => !r[1] && r[2]) && (
                  <div style={{ display: 'flex', gap: 5, fontSize: 10.5, color: C.amber, marginTop: 6 }}>
                    <AlertTriangle size={12} style={{ flex: '0 0 12px', marginTop: 1 }} />
                    A warehouse with stores but no bin stock can only be served across warehouses — or not at all under Own RDC only.
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="card">
            <div style={{ display: 'flex', flexWrap: 'wrap', borderBottom: `1px solid ${C.cardBorder}` }}>
              {rep.contents.map(c => (
                <button key={c.sheet} type="button" disabled={c.rows == null} onClick={() => setSheet(c.sheet)}
                  title={c.rows == null ? 'Needs a session' : c.desc}
                  style={{ padding: '7px 10px', border: 'none', background: 'none', cursor: c.rows == null ? 'default' : 'pointer', fontSize: 10.5,
                           fontWeight: 700, color: sheet === c.sheet ? C.primary : c.rows == null ? C.textMuted : C.textSub,
                           borderBottom: `2px solid ${sheet === c.sheet ? C.primary : 'transparent'}` }}>
                  {c.sheet.slice(0, 2)} · {c.title} <span style={{ fontWeight: 400, color: C.textMuted }}>{c.rows == null ? '—' : n(c.rows)}{c.capped ? ' (capped)' : ''}</span>
                </button>
              ))}
            </div>
            {p && (
              <div style={{ padding: 10, display: 'grid', gap: 8 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 11, color: C.textSub }}>{(rep.contents.find(c => c.sheet === sheet) || {}).desc}</span>
                  <span style={{ marginLeft: 'auto', fontSize: 10.5, color: C.textMuted }}>
                    {p.total > p.rows.length ? `showing ${n(p.rows.length)} of ${n(p.total)}` : `${n(p.total)} rows`}
                  </span>
                  <button type="button" className="btn-secondary btn-sm" onClick={() => dl(sheet)}><Download size={11} /> CSV</button>
                </div>
                <div style={{ overflowX: 'auto', maxHeight: 520, overflowY: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
                    <thead style={{ position: 'sticky', top: 0, background: C.card }}><tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
                      {p.columns.map(c => <th key={c} style={{ padding: '4px 7px', textAlign: 'left', whiteSpace: 'nowrap' }}>{c}</th>)}
                    </tr></thead>
                    <tbody>{p.rows.map((r, i) => (
                      <tr key={i} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
                        {r.map((v, j) => <td key={j} style={{ padding: '3px 7px', whiteSpace: 'nowrap', textAlign: typeof v === 'number' ? 'right' : 'left' }}>{typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : (v ?? '—')}</td>)}
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function Sub({ children }) {
  return <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: C.textMuted, marginBottom: 6 }}>{children}</div>
}

function Tile({ k, v, x, tone }) {
  const fg = tone === 'red' ? C.red : tone === 'amber' ? C.amber : C.text
  return (
    <div className="card" style={{ padding: '9px 11px', borderColor: tone === 'red' ? C.redBd : tone === 'amber' ? C.amberBd : undefined }}>
      <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '.07em', textTransform: 'uppercase', color: C.textMuted }}>{k}</div>
      <div style={{ fontSize: 19, fontWeight: 800, marginTop: 2, fontVariantNumeric: 'tabular-nums', color: fg }}>{v}</div>
      <div style={{ fontSize: 10, color: C.textSub, marginTop: 1 }}>{x}</div>
    </div>
  )
}
