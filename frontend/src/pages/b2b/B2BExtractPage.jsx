import { useEffect, useMemo, useState } from 'react'
import { Search, Download, Upload, X } from 'lucide-react'
import toast from 'react-hot-toast'
import { b2bAPI, saveBlob } from '@/services/api'
import { C } from '@/theme/colors'

const n = v => (v == null ? '—' : Math.round(+v).toLocaleString())
const when = s => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
const split = t => [...new Set(String(t || '').split(/[\s,;|]+/).map(x => x.trim()).filter(Boolean))]

export default function B2BExtractPage() {
  const [objects, setObjects] = useState([])
  const [obj, setObj] = useState('ARS_B2B_ALLOC')
  const [sessions, setSessions] = useState([])
  const [sid, setSid] = useState('')
  const [stores, setStores] = useState('')
  const [arts, setArts] = useState('')
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    b2bAPI.extractObjects().then(({ data: r }) => setObjects(r.data.objects)).catch(() => toast.error('Could not list the tables'))
    b2bAPI.sessions(60).then(({ data: r }) => {
      const real = r.data.sessions.filter(s => s.STATUS === 'DONE' && !s.DRY_RUN)
      setSessions(real)
      if (real[0]) setSid(String(real[0].SESSION_ID))
    }).catch(() => {})
  }, [])

  const meta = useMemo(() => objects.find(o => o.object === obj), [objects, obj])
  const sList = split(stores), aList = split(arts)
  const body = (format) => ({ object: obj, stores: sList, arts: aList, session_id: meta?.session_col && sid ? Number(sid) : null, format })

  const preview = async () => {
    setBusy(true)
    try { const { data: r } = await b2bAPI.extractPreview(body()); setRes(r.data) }
    catch (e) { toast.error(e.response?.data?.detail || 'Could not read the table') }
    finally { setBusy(false) }
  }
  const download = async (format) => {
    const t = toast.loading(`Preparing ${format.toUpperCase()}…`)
    try { const r = await b2bAPI.extractDownload(body(format)); toast.success(`Downloaded ${saveBlob(r, `extract.${format}`)}`, { id: t }) }
    catch (e) {
      let msg = 'Download failed'
      try { msg = JSON.parse(await e.response.data.text()).detail || msg } catch { /* not JSON */ }
      toast.error(msg, { id: t })
    }
  }
  useEffect(() => { setRes(null) }, [obj, sid])

  const owned = objects.filter(o => o.owned), sources = objects.filter(o => !o.owned)
  return (
    <div className="space-y-3">
      <div>
        <h1 className="page-title">Store × Article Extract</h1>
        <p className="page-subtitle">Paste or upload a store list and an article list; get every matching row of any GRT ALC table. Read only.</p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, .8fr) minmax(0, 1fr) minmax(0, 1fr)', gap: 10, alignItems: 'start' }}>
        <div className="card" style={{ padding: 10, display: 'grid', gap: 8 }}>
          <Sub>1 · Table</Sub>
          <select className="input" value={obj} onChange={e => setObj(e.target.value)}>
            <optgroup label="GRT ALC tables">{owned.map(o => <option key={o.object} value={o.object}>{o.title} · {o.object}</option>)}</optgroup>
            <optgroup label="Sources it reads">{sources.map(o => <option key={o.object} value={o.object}>{o.title} · {o.object}</option>)}</optgroup>
          </select>
          {meta && (
            <div style={{ fontSize: 10.5, color: C.textSub, lineHeight: 1.6 }}>
              <div>Store filter: {meta.store_col ? <b>{meta.store_col}</b> : <span style={{ color: C.amber }}>none — the store list is ignored</span>}</div>
              <div>Article filter: {meta.art_col ? <b>{meta.art_col}</b> : <span style={{ color: C.amber }}>none — the article list is ignored</span>}</div>
              <div>{meta.columns} columns{meta.rows != null ? ` · ${n(meta.rows)} rows in all` : ''}</div>
            </div>
          )}
          {meta?.session_col && (
            <label style={{ fontSize: 10.5, color: C.textSub }}>Session
              <select className="input" value={sid} onChange={e => setSid(e.target.value)}>
                <option value="">All sessions</option>
                {sessions.map(s => <option key={s.SESSION_ID} value={s.SESSION_ID}>Session {s.SESSION_ID} · {when(s.CREATED_AT)} · {n(s.UNITS_ALLOCATED)} units</option>)}
              </select>
            </label>
          )}
        </div>
        <CodeList title="2 · Stores" kind="store" value={stores} onChange={setStores} disabled={meta && !meta.store_col} />
        <CodeList title="3 · Articles" kind="art" value={arts} onChange={setArts} disabled={meta && !meta.art_col} />
      </div>

      <div className="card" style={{ padding: 10, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button type="button" className="btn-primary" onClick={preview} disabled={busy || !meta}><Search size={12} /> Count and preview</button>
        <span style={{ fontSize: 10.5, color: C.textMuted }}>
          {sList.length || 'No'} store code(s) · {aList.length || 'no'} article code(s). An empty list means no filter on that column.
        </span>
        {res && (
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
            <button type="button" className="btn-secondary btn-sm" onClick={() => download('csv')}><Download size={11} /> CSV · {n(res.total)} rows</button>
            <button type="button" className="btn-secondary btn-sm" onClick={() => download('xlsx')} disabled={!res.xlsx_ok}
              title={res.xlsx_ok ? '' : 'More rows than Excel holds — use CSV'}><Download size={11} /> Excel</button>
          </span>
        )}
      </div>

      {res && (
        <div className="card">
          <div style={{ padding: 10, borderBottom: `1px solid ${C.cardBorder}`, display: 'grid', gap: 6 }}>
            <div style={{ fontSize: 12, fontWeight: 700 }}>{n(res.total)} matching rows in {res.title}</div>
            {res.matched.map(m => (
              <div key={m.list} style={{ fontSize: 10.5, color: m.missing_count ? C.amber : C.textSub }}>
                <b>{m.list}:</b> {m.given ? `${n(m.given)} given, ${n(m.found)} found` : m.note}
                {m.given > 0 && m.note && !m.missing_count ? ` — ${m.note}` : ''}
                {m.missing_count > 0 && <> — <b>{n(m.missing_count)} not found</b> ({m.note}): <code style={{ color: C.codeColor }}>{m.missing.join(', ')}{m.missing_count > m.missing.length ? ' …' : ''}</code></>}
              </div>
            ))}
          </div>
          <div style={{ padding: 10, display: 'grid', gap: 6 }}>
            <div style={{ fontSize: 10.5, color: C.textMuted }}>{res.total > res.rows.length ? `First ${n(res.rows.length)} of ${n(res.total)} rows — download for all of them.` : `${n(res.total)} rows`}</div>
            <div style={{ overflowX: 'auto', maxHeight: 520, overflowY: 'auto' }}>
              <table style={{ borderCollapse: 'collapse', fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
                <thead style={{ position: 'sticky', top: 0, background: C.card }}><tr style={{ color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
                  {res.columns.map(c => <th key={c} style={{ padding: '4px 7px', textAlign: 'left', whiteSpace: 'nowrap' }}>{c}</th>)}
                </tr></thead>
                <tbody>{res.rows.map((r, i) => (
                  <tr key={i} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
                    {r.map((v, j) => <td key={j} style={{ padding: '3px 7px', whiteSpace: 'nowrap', textAlign: typeof v === 'number' ? 'right' : 'left' }}>{v == null ? '—' : String(v)}</td>)}
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function CodeList({ title, kind, value, onChange, disabled }) {
  const [file, setFile] = useState(null)        // {name, columns, used}
  const [raw, setRaw] = useState(null)
  const parse = async (f, column) => {
    const fd = new FormData()
    fd.append('file', f); fd.append('kind', kind); if (column) fd.append('column', column)
    try {
      const { data: r } = await b2bAPI.extractParse(fd)
      onChange(r.data.codes.join('\n'))
      setFile({ name: f.name, columns: r.data.columns, used: r.data.used })
      toast.success(`${r.data.codes.length.toLocaleString()} codes from ${f.name} (column ${r.data.used})`)
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not read the file') }
  }
  const count = split(value).length
  return (
    <div className="card" style={{ padding: 10, display: 'grid', gap: 6, opacity: disabled ? 0.55 : 1 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Sub>{title}</Sub>
        <span style={{ marginLeft: 'auto', fontSize: 10.5, color: C.textMuted }}>{count ? `${count.toLocaleString()} codes` : 'all'}</span>
        {value && <button type="button" className="btn-secondary btn-sm" onClick={() => { onChange(''); setFile(null) }} title="Clear"><X size={11} /></button>}
      </div>
      <textarea className="input" rows={6} value={value} onChange={e => onChange(e.target.value)} disabled={disabled}
        placeholder={`Paste ${kind === 'store' ? 'store codes' : 'article numbers'} — one a line, or separated by commas, spaces or tabs`}
        style={{ fontFamily: 'ui-monospace, Consolas, monospace', fontSize: 11, resize: 'vertical' }} />
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
        <label className="btn-secondary btn-sm" style={{ cursor: disabled ? 'default' : 'pointer' }}>
          <Upload size={11} /> Upload csv / xlsx
          <input type="file" accept=".csv,.txt,.xlsx,.xls,.xlsm" hidden disabled={disabled}
            onChange={e => { const f = e.target.files?.[0]; if (f) { setRaw(f); parse(f) } e.target.value = '' }} />
        </label>
        {file && file.columns.length > 1 && (
          <select className="input" value={file.used || ''} onChange={e => parse(raw, e.target.value)} style={{ width: 160 }} title="Which column holds the codes">
            {file.columns.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        )}
        {file && <span style={{ fontSize: 10, color: C.textMuted }}>{file.name}</span>}
      </div>
    </div>
  )
}

function Sub({ children }) {
  return <div style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: C.textMuted }}>{children}</div>
}
