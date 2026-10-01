import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { FolderOpen, Upload as UploadIcon, ShieldCheck, Loader2, X, Database, RefreshCw } from 'lucide-react'
import toast from 'react-hot-toast'
import { b2bAPI } from '@/services/api'
import { C } from '@/theme/colors'
import B2BStepper from '@/components/b2b/B2BStepper'
import CheckList from '@/components/b2b/CheckList'
import { Status } from '@/pages/b2b/B2BOverviewPage'

const n = v => (v == null ? '—' : Math.round(+v).toLocaleString())
const mb = b => (b == null ? '—' : `${(b / 1024 / 1024).toFixed(1)} MB`)
const when = s => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
const LIVE = ['QUEUED', 'CHECKING', 'LOADING']
const SHEETS = [
  { key: 'bin', label: 'Bin Master', table: 'ARS_B2B_BIN_MASTER' },
  { key: 'store', label: 'STORE MASTER', table: 'ARS_B2B_STORE_MASTER' },
  { key: 'req', label: 'REQ', table: 'ARS_B2B_REQ' },
]

export default function B2BUploadPage() {
  const [source, setSource] = useState('PATH')              // PATH | FILE
  const [folder, setFolder] = useState('')
  const [files, setFiles] = useState([])
  const [listing, setListing] = useState(false)
  const [path, setPath] = useState('')
  const [file, setFile] = useState(null)
  const [mode, setMode] = useState('OVERWRITE')
  const [sheets, setSheets] = useState(['bin', 'store', 'req'])
  const [binRdc, setBinRdc] = useState('')
  const [job, setJob] = useState(null)
  const [recent, setRecent] = useState([])
  const [tiles, setTiles] = useState(null)
  const [confirm, setConfirm] = useState(false)
  const lastArgs = useRef(null)
  const poll = useRef(null)

  const refresh = useCallback(async () => {
    try {
      const [u, o] = await Promise.all([b2bAPI.uploads(10), b2bAPI.overview()])
      setRecent(u.data.data.uploads || [])
      setTiles(o.data.data.tiles || null)
    } catch { /* the page still works without these */ }
  }, [])

  const listFolder = useCallback(async (f) => {
    setListing(true)
    try {
      const { data: r } = await b2bAPI.folder(f || undefined)
      setFolder(r.data.folder)
      setFiles(r.data.files || [])
      setPath(p => p || (r.data.files[0]?.path ?? ''))       // newest first
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Could not list that folder')
      setFiles([])
    } finally { setListing(false) }
  }, [])

  const watch = useCallback(async (id) => {
    clearInterval(poll.current)
    const tick = async () => {
      try {
        const { data: r } = await b2bAPI.upload(id)
        setJob(r.data)
        if (!LIVE.includes(r.data.STATUS)) {
          clearInterval(poll.current)
          if (r.data.STATUS === 'LOADED') { toast.success(r.data.PROGRESS); refresh() }
          if (r.data.STATUS === 'FAILED') toast.error(r.data.PROGRESS || 'Failed')
          if (r.data.STATUS === 'CHECKED') refresh()
        }
      } catch { clearInterval(poll.current) }
    }
    await tick()
    poll.current = setInterval(tick, 1500)
  }, [refresh])

  useEffect(() => {
    (async () => {
      try {
        const { data: r } = await b2bAPI.uploadDefaults()
        if (r.data.running) watch(r.data.running)            // resume a job after a refresh
      } catch { /* ignore */ }
      listFolder()
      refresh()
    })()
    return () => clearInterval(poll.current)
  }, [listFolder, refresh, watch])

  const picked = useMemo(() => files.find(f => f.path === path), [files, path])
  const detected = source === 'PATH' ? picked?.warehouse : (file?.name.match(/(?<![A-Z0-9])([A-Z]{2}\d{2})(?![A-Z0-9])/i)?.[1]?.toUpperCase())
  const running = job && LIVE.includes(job.STATUS)

  const check = async (extra = {}) => {
    const args = { source, path, file, mode, sheets, bin_rdc: binRdc || null, swap_store_cols: false,
                   ...(lastArgs.current && extra.keep ? lastArgs.current : {}), ...extra }
    delete args.keep
    if (!args.sheets.length) return toast.error('Choose at least one sheet')
    if (args.source === 'PATH' && !args.path) return toast.error('Choose a workbook')
    if (args.source === 'FILE' && !args.file) return toast.error('Choose a file')
    lastArgs.current = args
    setConfirm(false)
    try {
      const { data: r } = args.source === 'PATH'
        ? await b2bAPI.checkPath({ path: args.path, mode: args.mode, sheets: args.sheets,
                                   bin_rdc: args.bin_rdc, swap_store_cols: args.swap_store_cols })
        : await b2bAPI.checkFile(args.file, args)
      watch(r.data.upload_id)
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not start the check') }
  }

  const load = async () => {
    setConfirm(false)
    try { await b2bAPI.load(job.UPLOAD_ID); watch(job.UPLOAD_ID) }
    catch (e) { toast.error(e.response?.data?.detail || 'Could not start the load'); watch(job.UPLOAD_ID) }
  }

  const cancel = async () => {
    try { const { data: r } = await b2bAPI.cancel(job.UPLOAD_ID); toast(r.message) }
    catch (e) { toast.error(e.response?.data?.detail || 'Could not cancel') }
  }

  const checks = job?.checks
  const canLoad = job?.STATUS === 'CHECKED' && (job.BLOCKING || 0) === 0
  const jobSheets = (job?.SHEETS || '').split(',').filter(Boolean)
  const current = { bin: tiles?.bin.rows, store: tiles?.store.rows, req: tiles?.req.rows }
  const incoming = { bin: job?.ROWS_BIN, store: job?.ROWS_STORE, req: job?.ROWS_REQ }

  const actions = {
    STORE_SWAP: it => it.level === 'block' && (
      <button type="button" className="btn-primary btn-sm" disabled={running}
        onClick={() => check({ keep: true, swap_store_cols: true })}>
        Swap them for this load and check again
      </button>),
    BIN_WAREHOUSE: it => it.level === 'block' && (
      <WarehousePick onPick={w => { setBinRdc(w); check({ keep: true, bin_rdc: w }) }} disabled={running} />),
  }

  return (
    <div className="space-y-3">
      <div>
        <h1 className="page-title">1 · Upload Data</h1>
        <p className="page-subtitle">
          One workbook, three sheets, three tables. Check the file first — nothing is written until you load.
        </p>
      </div>
      <B2BStepper current="upload" />

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 10 }}>
        {/* ── source + options ── */}
        <div className="card">
          <Head title="Workbook" />
          <div style={{ padding: 10, display: 'grid', gap: 10 }}>
            <Seg value={source} onChange={setSource}
              opts={[['PATH', 'Network folder', FolderOpen], ['FILE', 'Upload a file', UploadIcon]]} />

            {source === 'PATH' ? (
              <div style={{ display: 'grid', gap: 6 }}>
                <div style={{ display: 'flex', gap: 6 }}>
                  <input className="input" value={folder} onChange={e => setFolder(e.target.value)}
                    style={{ fontFamily: 'ui-monospace, Consolas, monospace', fontSize: 10 }}
                    aria-label="Folder" id="b2b-folder" />
                  <button type="button" className="btn-secondary" onClick={() => listFolder(folder)} disabled={listing}>
                    <RefreshCw size={12} className={listing ? 'animate-spin' : ''} /> List
                  </button>
                </div>
                <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 6, maxHeight: 190, overflowY: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5 }}>
                    <thead>
                      <tr style={{ background: C.headerBg, color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
                        <th style={{ width: 22 }} /><th style={{ textAlign: 'left', padding: '4px 6px' }}>Workbook</th>
                        <th style={{ padding: '4px 6px' }}>RDC</th><th style={{ textAlign: 'right', padding: '4px 6px' }}>Size</th>
                        <th style={{ textAlign: 'right', padding: '4px 8px' }}>Modified</th>
                      </tr>
                    </thead>
                    <tbody>
                      {files.map((f, i) => (
                        <tr key={f.path} onClick={() => setPath(f.path)}
                          style={{ cursor: 'pointer', borderTop: `1px solid ${C.cardBorder}`,
                                   background: f.path === path ? C.primaryLt : undefined }}>
                          <td style={{ textAlign: 'center' }}>
                            <input type="radio" name="b2b-wb" checked={f.path === path} onChange={() => setPath(f.path)}
                              aria-label={f.name} />
                          </td>
                          <td style={{ padding: '4px 6px', fontWeight: f.path === path ? 700 : 500 }}>
                            {f.name}
                            {i === 0 && <span className="badge" style={{ marginLeft: 6, background: C.greenBg, color: C.green }}>newest</span>}
                            {f.is_default && <span className="badge" style={{ marginLeft: 6, background: C.amberBg, color: C.amber }}
                              title="The Streamlit tool's configured default">tool default</span>}
                          </td>
                          <td style={{ padding: '4px 6px', textAlign: 'center' }}>{f.warehouse || '—'}</td>
                          <td style={{ padding: '4px 6px', textAlign: 'right', color: C.textSub }}>{mb(f.bytes)}</td>
                          <td style={{ padding: '4px 8px', textAlign: 'right', color: C.textSub, whiteSpace: 'nowrap' }}>{when(f.modified)}</td>
                        </tr>
                      ))}
                      {!files.length && (
                        <tr><td colSpan={5} style={{ padding: 10, color: C.textMuted }}>
                          {listing ? 'Listing…' : 'No workbooks here, or the folder cannot be reached'}
                        </td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <input type="file" accept=".xlsx,.xlsm,.xls" id="b2b-file"
                onChange={e => setFile(e.target.files?.[0] || null)} style={{ fontSize: 11 }} />
            )}

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <Field label="Mode">
                <Seg value={mode} onChange={setMode} opts={[['OVERWRITE', 'Overwrite'], ['APPEND', 'Append']]} />
              </Field>
              <Field label="Warehouse of these bins">
                <input className="input" id="b2b-binrdc" value={binRdc} onChange={e => setBinRdc(e.target.value.toUpperCase())}
                  placeholder={detected ? `${detected} (from the file name)` : 'e.g. DH24'} maxLength={4} />
              </Field>
            </div>
            {mode === 'APPEND' && (
              <Note tone="amber">
                <b>Append adds rows on top</b> and never removes duplicates. Use it only to add a second
                warehouse's extract — usually its Bin Master and Store Master, not its REQ.
              </Note>
            )}

            <Field label="Sheets to load">
              <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
                {SHEETS.map(s => (
                  <label key={s.key} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11 }}>
                    <input type="checkbox" checked={sheets.includes(s.key)}
                      onChange={e => setSheets(v => e.target.checked ? [...v, s.key] : v.filter(x => x !== s.key))} />
                    {s.label}
                  </label>
                ))}
              </div>
            </Field>

            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <button type="button" className="btn-primary" onClick={() => check()} disabled={running}>
                <ShieldCheck size={12} /> Check file
              </button>
              <span style={{ fontSize: 10, color: C.textMuted }}>Reads and checks every row. Writes nothing.</span>
            </div>
          </div>
        </div>

        {/* ── check result ── */}
        <div className="card">
          <Head title="Check result"
            right={job ? <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>upload {job.UPLOAD_ID} <Status s={job.STATUS} /></span>
                       : 'nothing checked yet'} />
          <div style={{ padding: 10, display: 'grid', gap: 10 }}>
            {!job && <div style={{ fontSize: 11, color: C.textMuted }}>Choose a workbook and press <b>Check file</b>.</div>}

            {running && (
              <div style={{ display: 'grid', gap: 6 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }}>
                  <span style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                    <Loader2 size={13} className="animate-spin" style={{ color: C.primary }} /> {job.PROGRESS}
                  </span>
                  <span style={{ color: C.textMuted }}>{job.PROGRESS_PCT || 0}%</span>
                </div>
                <div style={{ height: 7, borderRadius: 4, background: C.grayBd, overflow: 'hidden' }}>
                  <div style={{ width: `${job.PROGRESS_PCT || 0}%`, height: '100%', background: C.primary, transition: 'width .4s' }} />
                </div>
                <div><button type="button" className="btn-secondary btn-sm" onClick={cancel}><X size={11} /> Cancel</button></div>
                {job.STATUS === 'LOADING' && (
                  <Note tone="blue">Everything is written in one transaction. If this is cancelled or fails, every table stays exactly as it was.</Note>
                )}
              </div>
            )}

            {job?.STATUS === 'FAILED' && <Note tone="red"><b>{job.PROGRESS}</b><br />{String(job.ERROR || '').split('\n')[0]}</Note>}
            {job?.STATUS === 'EXPIRED' && <Note tone="amber">{job.PROGRESS}</Note>}
            {job?.STATUS === 'LOADED' && <Note tone="green"><b>{job.PROGRESS}.</b> Next: rebuild the demand table in <a href="/bin-alloc/mbq" style={{ color: C.primary, fontWeight: 700 }}>3 · Build MBQ</a>.</Note>}

            {checks?.summary && !running && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 6 }}>
                {SHEETS.map(s => {
                  const sm = checks.summary[s.key]
                  return (
                    <div key={s.key} style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 6, padding: '7px 9px',
                                              opacity: sm ? 1 : .45 }}>
                      <div style={{ fontSize: 11, fontWeight: 700 }}>{s.label}</div>
                      <div style={{ fontSize: 9.5, color: C.textMuted, fontFamily: 'ui-monospace, Consolas, monospace' }}>→ {s.table}</div>
                      <div style={{ fontSize: 17, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>{sm ? n(sm.rows) : '—'}</div>
                      <div style={{ fontSize: 10, color: C.textSub }}>
                        {s.key === 'bin' && sm && `${n(sm.qty)} pcs · ${Object.keys(sm.by_rdc || {}).join(', ')}`}
                        {s.key === 'store' && sm && Object.entries(sm.by_rdc || {}).map(([k, v]) => `${k} ${v}`).join(' · ')}
                        {s.key === 'req' && sm && `${n(sm.units)} units · ${n(sm.stores)} stores`}
                        {!sm && 'not in this upload'}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}

            {checks?.issues && !running && <CheckList items={checks.issues} actions={actions} />}

            {canLoad && !confirm && (
              <button type="button" className="btn-primary" onClick={() => setConfirm(true)}>
                <Database size={12} /> Load {jobSheets.length} sheet{jobSheets.length === 1 ? '' : 's'}…
              </button>
            )}
            {canLoad && confirm && (
              <div style={{ border: `1px solid ${C.primaryBd}`, background: C.primaryLt, borderRadius: 6, padding: 10, display: 'grid', gap: 8 }}>
                <div style={{ fontSize: 11, fontWeight: 700 }}>
                  {job.MODE === 'APPEND' ? 'Add these rows to what is loaded?' : 'Replace what is loaded?'}
                </div>
                <table style={{ fontSize: 10.5, borderCollapse: 'collapse' }}>
                  <tbody style={{ fontVariantNumeric: 'tabular-nums' }}>
                    {SHEETS.filter(s => jobSheets.includes(s.key)).map(s => (
                      <tr key={s.key}>
                        <td style={{ padding: '2px 12px 2px 0' }}>{s.label}</td>
                        <td style={{ padding: '2px 8px', textAlign: 'right', color: C.textSub }}>{n(current[s.key])} now</td>
                        <td style={{ padding: '2px 8px' }}>→</td>
                        <td style={{ padding: '2px 0', textAlign: 'right', fontWeight: 700 }}>
                          {job.MODE === 'APPEND' ? `${n((current[s.key] || 0) + (incoming[s.key] || 0))} after` : `${n(incoming[s.key])} after`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div style={{ fontSize: 10, color: C.textSub }}>
                  Not touched: sheets you did not choose, settings, and the Streamlit tool's own B2B_* tables.
                  All chosen sheets go in one transaction.
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button type="button" className="btn-primary" onClick={load}><Database size={12} /> Load now</button>
                  <button type="button" className="btn-secondary" onClick={() => setConfirm(false)}>Cancel</button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="card">
        <Head title="Recent uploads" />
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10.5, minWidth: 760 }}>
            <thead>
              <tr style={{ background: C.headerBg, color: C.textMuted, fontSize: 9.5, textTransform: 'uppercase' }}>
                {['#', 'Status', 'Workbook', 'Mode', 'Bins', 'Stores', 'REQ', 'Checks', 'By', 'When', 'Read', 'Load']
                  .map(h => <th key={h} style={{ textAlign: 'left', padding: '5px 8px', fontWeight: 700 }}>{h}</th>)}
              </tr>
            </thead>
            <tbody style={{ fontVariantNumeric: 'tabular-nums' }}>
              {recent.map(u => (
                <tr key={u.UPLOAD_ID} onClick={() => watch(u.UPLOAD_ID)} style={{ cursor: 'pointer', borderTop: `1px solid ${C.cardBorder}`,
                  background: job?.UPLOAD_ID === u.UPLOAD_ID ? C.primaryLt : undefined }}>
                  <td style={{ padding: '5px 8px', fontWeight: 700 }}>{u.UPLOAD_ID}</td>
                  <td style={{ padding: '5px 8px' }}><Status s={u.STATUS} /></td>
                  <td style={{ padding: '5px 8px', maxWidth: 230, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                    title={u.SOURCE_NAME}>{String(u.SOURCE_NAME || '').split(/[\\/]/).pop()}</td>
                  <td style={{ padding: '5px 8px' }}>{u.MODE}</td>
                  <td style={{ padding: '5px 8px' }}>{n(u.ROWS_BIN)}</td>
                  <td style={{ padding: '5px 8px' }}>{n(u.ROWS_STORE)}</td>
                  <td style={{ padding: '5px 8px' }}>{n(u.ROWS_REQ)}</td>
                  <td style={{ padding: '5px 8px' }}>
                    {u.BLOCKING ? <span style={{ color: C.red, fontWeight: 700 }}>{u.BLOCKING} to fix</span>
                      : u.BLOCKING === 0 ? <span style={{ color: C.green }}>ok</span> : '—'}
                    {u.WARNINGS ? <span style={{ color: C.amber }}> · {u.WARNINGS} warn</span> : ''}
                  </td>
                  <td style={{ padding: '5px 8px', color: C.textSub }}>{u.LOADED_BY || u.CREATED_BY}</td>
                  <td style={{ padding: '5px 8px', color: C.textSub, whiteSpace: 'nowrap' }}>{when(u.LOADED_AT || u.CHECKED_AT || u.CREATED_AT)}</td>
                  <td style={{ padding: '5px 8px', color: C.textSub }}>{u.READ_SEC != null ? `${Math.round(u.READ_SEC)}s` : '—'}</td>
                  <td style={{ padding: '5px 8px', color: C.textSub }}>{u.LOAD_SEC != null ? `${Math.round(u.LOAD_SEC)}s` : '—'}</td>
                </tr>
              ))}
              {!recent.length && <tr><td colSpan={12} style={{ padding: 10, color: C.textMuted }}>No uploads yet</td></tr>}
            </tbody>
          </table>
        </div>
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

function Field({ label, children }) {
  return (
    <div style={{ display: 'grid', gap: 4 }}>
      <span style={{ fontSize: 10, fontWeight: 700, color: C.textSub }}>{label}</span>
      {children}
    </div>
  )
}

function Seg({ value, onChange, opts }) {
  return (
    <div style={{ display: 'inline-flex', border: `1px solid ${C.inputBd}`, borderRadius: 6, overflow: 'hidden', width: 'fit-content' }}>
      {opts.map(([v, label, Icon], i) => (
        <button key={v} type="button" onClick={() => onChange(v)} aria-pressed={value === v}
          style={{ padding: '4px 11px', fontSize: 10.5, fontWeight: 600, border: 'none', cursor: 'pointer',
                   borderLeft: i ? `1px solid ${C.inputBd}` : 'none', display: 'flex', alignItems: 'center', gap: 5,
                   background: value === v ? C.primary : C.card, color: value === v ? '#fff' : C.textSub }}>
          {Icon && <Icon size={12} />}{label}
        </button>
      ))}
    </div>
  )
}

function Note({ tone, children }) {
  const t = { amber: [C.amberBg, C.amberBd], blue: [C.blueBg, C.blueBd], red: [C.redBg, C.redBd], green: [C.greenBg, C.greenBd] }[tone]
  return <div style={{ fontSize: 10.5, color: C.textSub, background: t[0], border: `1px solid ${t[1]}`, borderRadius: 6, padding: '6px 9px', lineHeight: 1.45 }}>{children}</div>
}

function WarehousePick({ onPick, disabled }) {
  const [w, setW] = useState('DH24')
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <select className="input" style={{ width: 90 }} value={w} onChange={e => setW(e.target.value)} aria-label="Warehouse">
        <option>DH24</option><option>DW01</option>
      </select>
      <button type="button" className="btn-primary btn-sm" disabled={disabled} onClick={() => onPick(w)}>Use it and check again</button>
    </span>
  )
}
