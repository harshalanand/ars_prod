import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  RefreshCw, Plus, Play, Pencil, Trash2, Power, X, FlaskConical, RotateCcw, Clock, History,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { getDataAPI } from '@/services/api'
import {
  fmtNum, fmtIST, scheduleText, StatusPill, MODE_LABEL, SnowflakeBanner, CheckList, errText,
  useGetDataPerms,
} from '@/components/getdata/gdUtils'

const WD = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const blank = {
  job_name: '', description: '', source_object: '', target_table: '', load_mode: 'replace',
  key_cols: '', watermark_col: '', trigger_type: 'schedule', freq: 'daily', times: ['07:00'],
  weekdays: [0], days: '1', every_n_hours: 6, win_start: '', win_end: '',
  retry_on_fail: true, retry_delay_min: 15, enabled: true, loader: 'bulk', allow_empty: false,
  column_map: [],
}

// Column mapping: [{ source, name, load }] — rename / skip Snowflake columns.
const LOCAL_NAME = /^[A-Za-z][A-Za-z0-9_]{0,119}$/

// Keep saved names/choices for columns still in Snowflake; new ones start as-is.
const mergeColumns = (cols, map) => {
  const by = Object.fromEntries((map || []).map(e => [e.source.toUpperCase(), e]))
  return cols.map(c => {
    const e = by[c.name.toUpperCase()]
    return { source: c.name, name: e ? e.name : c.name, load: e ? e.load !== false : true }
  })
}

// Same rules as the server (get_data_sync_service._clean_column_map).
const columnIssues = (map, keyList, wm) => {
  const row = {}, general = []
  const seen = {}
  const protectedCols = new Set([...keyList, ...(wm ? [wm.toUpperCase()] : [])])
  for (const e of map) {
    const src = e.source.toUpperCase()
    if (!e.load) {
      if (protectedCols.has(src)) row[src] = 'Key and watermark columns can’t be skipped'
      continue
    }
    const n = (e.name || '').trim()
    if (!LOCAL_NAME.test(n)) row[src] = 'Letters, digits and _ only, starting with a letter'
    else if (seen[n.toUpperCase()]) row[src] = `“${n}” is already used for ${seen[n.toUpperCase()]}`
    else seen[n.toUpperCase()] = e.source
  }
  if (map.length && !map.some(e => e.load)) general.push('Load at least one column')
  return { row, general, any: Object.keys(row).length > 0 || general.length > 0 }
}

const mapSummary = (map) => {
  const m = map || []
  return { renamed: m.filter(e => e.load !== false && e.name !== e.source).length,
           skipped: m.filter(e => e.load === false).length }
}

const suggestTarget = (fq) => {
  const name = String(fq || '').split('.').pop().toUpperCase().replace(/^V_GD_/, '').replace(/^V_/, '')
  return name ? `GD_SF_${name.replace(/[^A-Z0-9_]/g, '_')}` : ''
}

export default function GetDataJobsPage() {
  const { canManage, canRun } = useGetDataPerms()
  const [params, setParams] = useSearchParams()
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [status, setStatus] = useState(null)
  const [editing, setEditing] = useState(null)
  const [deleting, setDeleting] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const timer = useRef(null)

  const load = async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const { data } = await getDataAPI.listJobs()
      setItems(data.data.items || [])
    } catch (e) {
      if (!quiet) toast.error(errText(e, 'Could not load jobs'))
    } finally { setLoading(false) }
  }

  useEffect(() => {
    load()
    getDataAPI.sfStatus().then(({ data }) => setStatus(data.data)).catch(() => {})
  }, [])

  // Arriving from Views with ?source=… opens a prefilled new job.
  useEffect(() => {
    const src = params.get('source')
    if (src && canManage) {
      setEditing({ ...blank, source_object: src, target_table: suggestTarget(src) })
      params.delete('source'); setParams(params, { replace: true })
    }
  }, [params, canManage])

  useEffect(() => {
    clearTimeout(timer.current)
    const busy = items.some(j => j.LOCK_RUN_ID)
    timer.current = setTimeout(() => load(true), busy ? 4000 : 30000)
    return () => clearTimeout(timer.current)
  }, [items])

  const openEdit = (j) => {
    const c = j.SCHEDULE_CONFIG || {}
    setEditing({
      job_id: j.JOB_ID, job_name: j.JOB_NAME || '', description: j.DESCRIPTION || '',
      source_object: j.SOURCE_OBJECT || '', target_table: j.TARGET_TABLE || '',
      load_mode: j.LOAD_MODE || 'replace', key_cols: (j.KEY_COLS || []).join(', '),
      watermark_col: j.WATERMARK_COL || '', watermark_value: j.WATERMARK_VALUE,
      trigger_type: j.TRIGGER_TYPE || 'manual', freq: c.freq || 'daily',
      times: c.times?.length ? c.times : ['07:00'], weekdays: c.weekdays?.length ? c.weekdays : [0],
      days: (c.days || [1]).join(', '), every_n_hours: c.every_n_hours || 6,
      win_start: c.start || '', win_end: c.end || '',
      retry_on_fail: !!j.RETRY_ON_FAIL, retry_delay_min: j.RETRY_DELAY_MIN || 15, enabled: !!j.ENABLED,
      loader: j.LOADER_PREF || 'bulk', allow_empty: !!j.ALLOW_EMPTY,
      column_map: (j.COLUMN_MAP || []).map(e => ({ source: e.source, name: e.name, load: e.load !== false })),
    })
  }

  const run = async (j, fullReload = false) => {
    if (fullReload && !confirm(`Full reload of "${j.JOB_NAME}"? It re-reads the whole source, replaces ${j.TARGET_TABLE}, and resets the watermark.`)) return
    setBusyId(j.JOB_ID)
    try {
      const { data } = await getDataAPI.runJob(j.JOB_ID, fullReload)
      toast.success(data.message || 'Run started')
      load(true)
    } catch (e) {
      toast.error(errText(e, 'Could not start the run'))
    } finally { setBusyId(null) }
  }

  const toggle = async (j) => {
    try { await getDataAPI.enableJob(j.JOB_ID, !j.ENABLED); load(true) }
    catch (e) { toast.error(errText(e)) }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><RefreshCw size={24} /> Sync Jobs</h1>
          <p className="text-gray-500 text-sm mt-0.5">Copy a Snowflake view or table into a local <span className="font-mono">GD_SF_</span> table in Rep_data, on a schedule (IST) or with Run now.</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => load()} className="btn-secondary"><RefreshCw size={16} className={loading ? 'animate-spin' : ''} /> Refresh</button>
          {canManage && <button onClick={() => setEditing({ ...blank })} className="btn-primary"><Plus size={16} /> New job</button>}
        </div>
      </div>

      <SnowflakeBanner status={status} />

      {!loading && items.length === 0 ? (
        <div className="card p-12 text-center text-gray-500 text-sm">
          <RefreshCw size={32} className="mx-auto mb-2 text-gray-300" />
          No sync jobs yet. {canManage && <>Create one with “New job”, or pick a view on <Link to="/get-data/snowflake/views" className="text-primary-600 underline">Snowflake Views</Link>.</>}
        </div>
      ) : (
        <div className="card p-0 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-600 text-xs uppercase">
                <tr>
                  <th className="px-4 py-2 text-left">Job</th>
                  <th className="px-4 py-2 text-left">Source → local table</th>
                  <th className="px-4 py-2 text-left">Mode</th>
                  <th className="px-4 py-2 text-left">Schedule (IST)</th>
                  <th className="px-4 py-2 text-left">Last run</th>
                  <th className="px-4 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.map(j => (
                  <tr key={j.JOB_ID} className="hover:bg-gray-50 align-top">
                    <td className="px-4 py-2.5">
                      <div className="font-medium text-gray-900 flex items-center gap-2">
                        {j.JOB_NAME}
                        {!j.ENABLED && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">disabled</span>}
                      </div>
                      {j.DESCRIPTION && <div className="text-xs text-gray-400">{j.DESCRIPTION}</div>}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="font-mono text-xs text-gray-700 break-all">{j.SOURCE_OBJECT}</div>
                      <div className="font-mono text-xs text-gray-400">→ {j.TARGET_TABLE}</div>
                    </td>
                    <td className="px-4 py-2.5 text-xs">
                      {MODE_LABEL[j.LOAD_MODE] || j.LOAD_MODE}
                      <span className="text-gray-400"> · {j.LOADER_PREF === 'classic' ? 'classic' : 'bulk'}</span>
                      {j.COLUMN_MAP?.length > 0 && (() => {
                        const s = mapSummary(j.COLUMN_MAP)
                        return <div className="text-primary-700">{s.renamed} renamed{s.skipped ? ` · ${s.skipped} skipped` : ''}</div>
                      })()}
                      {j.LOAD_MODE === 'incremental' && (
                        <div className="text-gray-400">on {j.WATERMARK_COL}{j.WATERMARK_VALUE ? ` ≥ ${j.WATERMARK_VALUE}` : ' · first run loads all'}</div>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-xs">
                      <div className="text-gray-700">{scheduleText(j)}</div>
                      {j.ENABLED && j.RETRY_AT && <div className="text-amber-700">retry {fmtIST(j.RETRY_AT)}</div>}
                      {j.ENABLED && j.TRIGGER_TYPE === 'schedule' && j.NEXT_RUN_AT && <div className="text-gray-400">next {fmtIST(j.NEXT_RUN_AT)}</div>}
                    </td>
                    <td className="px-4 py-2.5 text-xs min-w-[170px]">
                      {j.LOCK_RUN_ID ? <Progress j={j} /> : j.LAST_STATUS ? (
                        <div>
                          <StatusPill status={j.LAST_STATUS}>{j.LAST_STATUS}{j.LAST_STATUS === 'success' && j.LAST_ROWS != null ? ` · ${fmtNum(j.LAST_ROWS)}` : ''}</StatusPill>
                          <div className="text-gray-400 mt-0.5">{fmtIST(j.LAST_RUN_AT)}</div>
                          {j.LAST_STATUS === 'failed' && <div className="text-red-600 line-clamp-2 max-w-xs" title={j.LAST_MESSAGE || ''}>{j.LAST_MESSAGE}</div>}
                        </div>
                      ) : <span className="text-gray-400">never run</span>}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-1">
                        {canRun && (
                          <button onClick={() => run(j)} disabled={busyId === j.JOB_ID || !!j.LOCK_RUN_ID} title="Run now"
                            className="p-1.5 text-primary-600 hover:bg-primary-50 rounded disabled:opacity-40">
                            {busyId === j.JOB_ID ? <RefreshCw size={15} className="animate-spin" /> : <Play size={15} />}
                          </button>
                        )}
                        {canRun && j.LOAD_MODE === 'incremental' && (
                          <button onClick={() => run(j, true)} disabled={!!j.LOCK_RUN_ID} title="Full reload (resets the watermark)"
                            className="p-1.5 text-gray-500 hover:bg-gray-100 rounded disabled:opacity-40"><RotateCcw size={15} /></button>
                        )}
                        <Link to={`/get-data/runs?job_id=${j.JOB_ID}`} title="Run history" className="p-1.5 text-gray-500 hover:bg-gray-100 rounded"><History size={15} /></Link>
                        {canManage && <>
                          <button onClick={() => toggle(j)} title={j.ENABLED ? 'Disable' : 'Enable'}
                            className={`p-1.5 rounded hover:bg-gray-100 ${j.ENABLED ? 'text-green-600' : 'text-gray-400'}`}><Power size={15} /></button>
                          <button onClick={() => openEdit(j)} title="Edit" className="p-1.5 text-gray-500 hover:bg-gray-100 rounded"><Pencil size={15} /></button>
                          <button onClick={() => setDeleting(j)} title="Delete" disabled={!!j.LOCK_RUN_ID}
                            className="p-1.5 text-red-500 hover:bg-red-50 rounded disabled:opacity-40"><Trash2 size={15} /></button>
                        </>}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {editing && <JobModal initial={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(true) }} />}
      {deleting && <DeleteDialog job={deleting} onClose={() => setDeleting(null)} onDone={() => { setDeleting(null); load(true) }} />}
    </div>
  )
}

function Progress({ j }) {
  const pct = j.RUN_SOURCE_ROWS ? Math.min(100, Math.round((j.RUN_ROWS_LOADED || 0) / j.RUN_SOURCE_ROWS * 100)) : null
  return (
    <div>
      <StatusPill status="running">running · #{j.LOCK_RUN_ID}</StatusPill>
      <div className="text-gray-600 mt-1">{j.RUN_STEP || 'Starting'}</div>
      {j.RUN_SOURCE_ROWS != null && (
        <>
          <div className="h-1.5 bg-gray-100 rounded mt-1 overflow-hidden"><div className="h-full bg-amber-400" style={{ width: `${pct}%` }} /></div>
          <div className="text-gray-400 mt-0.5">{fmtNum(j.RUN_ROWS_LOADED || 0)} of {fmtNum(j.RUN_SOURCE_ROWS)}</div>
        </>
      )}
    </div>
  )
}

function JobModal({ initial, onClose, onSaved }) {
  const [f, setF] = useState(initial)
  const [views, setViews] = useState([])
  const [test, setTest] = useState(null)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState(null)
  const [targetTouched, setTargetTouched] = useState(!!initial.job_id || !!initial.target_table)
  const set = (k, v) => { setF(p => ({ ...p, [k]: v })); setErr(null) }

  useEffect(() => {
    getDataAPI.listViews().then(({ data }) => setViews(data.data.items || [])).catch(() => {})
  }, [])

  const [mapNote, setMapNote] = useState(null)

  const setSource = (v) => {
    // A different source has different columns — its mapping starts over.
    setF(p => ({ ...p, source_object: v, target_table: targetTouched ? p.target_table : suggestTarget(v),
                 column_map: [] }))
    setTest(null); setMapNote(null)
  }

  const runTest = async () => {
    if (!f.source_object.trim()) { setErr('Pick a source first'); return }
    setTesting(true); setTest(null)
    try {
      const { data } = await getDataAPI.testJob({ source_object: f.source_object, load_mode: f.load_mode,
        key_cols: f.key_cols, watermark_col: f.watermark_col })
      setTest(data.data)
      const cols = data.data.columns || []
      if (cols.length) {
        const present = new Set(cols.map(c => c.name.toUpperCase()))
        const gone = (f.column_map || []).filter(e => !present.has(e.source.toUpperCase())).map(e => e.source)
        setMapNote(gone.length ? `No longer in Snowflake, removed from the list: ${gone.join(', ')}` : null)
        setF(p => ({ ...p, column_map: mergeColumns(cols, p.column_map) }))
      }
    } catch (e) {
      setTest({ ok: false, checks: [{ label: 'Check', ok: false, detail: errText(e) }], columns: [] })
    } finally { setTesting(false) }
  }

  const keyList = String(f.key_cols || '').split(/[,\s]+/).filter(Boolean).map(k => k.toUpperCase())
  const toggleKey = (name) => {
    const u = name.toUpperCase()
    const next = keyList.includes(u) ? keyList.filter(k => k !== u) : [...keyList, u]
    set('key_cols', next.join(', '))
  }

  const payload = () => {
    let schedule_config = null
    if (f.trigger_type === 'schedule') {
      if (f.freq === 'every_n_hours') {
        schedule_config = { freq: 'every_n_hours', every_n_hours: Number(f.every_n_hours) || 6,
          ...(f.win_start && f.win_end ? { start: f.win_start, end: f.win_end } : {}) }
      } else {
        schedule_config = { freq: f.freq, times: f.times.filter(Boolean) }
        if (f.freq === 'weekly') schedule_config.weekdays = f.weekdays
        if (f.freq === 'monthly') schedule_config.days = String(f.days).split(/[,\s]+/).filter(Boolean).map(Number)
      }
    }
    return {
      job_name: f.job_name, description: f.description, source_object: f.source_object.trim(),
      target_table: f.target_table, load_mode: f.load_mode, key_cols: keyList,
      watermark_col: f.load_mode === 'incremental' ? f.watermark_col : null,
      trigger_type: f.trigger_type, schedule_config,
      retry_on_fail: !!f.retry_on_fail, retry_delay_min: Number(f.retry_delay_min) || 15, enabled: !!f.enabled,
      loader: f.loader || 'bulk', allow_empty: !!f.allow_empty,
      // Only a mapping that changes something is stored; otherwise columns keep Snowflake's names.
      column_map: (f.column_map || []).some(e => !e.load || e.name !== e.source)
        ? f.column_map.map(({ source, name, load }) => ({ source, name: (name || '').trim() || source, load: !!load }))
        : null,
    }
  }

  const issues = columnIssues(f.column_map || [], keyList, f.load_mode === 'incremental' ? f.watermark_col : '')

  const save = async () => {
    if (!f.job_name.trim()) { setErr('Give the job a name'); return }
    if (!f.source_object.trim()) { setErr('Pick a source'); return }
    if (!f.target_table.trim()) { setErr('Name the local table'); return }
    if (f.load_mode === 'incremental' && (!keyList.length || !f.watermark_col)) { setErr('Incremental needs key column(s) and a watermark column'); return }
    if (f.trigger_type === 'schedule' && f.freq !== 'every_n_hours' && !f.times.filter(Boolean).length) { setErr('Add at least one run time'); return }
    if (issues.any) { setErr('Fix the column names marked in red under Columns'); return }
    setSaving(true)
    try {
      if (f.job_id) await getDataAPI.updateJob(f.job_id, payload())
      else await getDataAPI.createJob(payload())
      toast.success(f.job_id ? 'Job updated' : 'Job created')
      onSaved()
    } catch (e) {
      setErr(errText(e, 'Save failed'))
    } finally { setSaving(false) }
  }

  const cols = test?.columns || []
  const moduleViews = views.filter(v => v.in_snowflake !== false).map(v => v.object)

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-3xl max-h-[92vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b sticky top-0 bg-white z-10">
          <h3 className="font-semibold text-gray-900">{f.job_id ? `Edit job · ${initial.job_name}` : 'New sync job'}</h3>
          <button onClick={onClose} className="p-1 text-gray-400 hover:text-gray-700"><X size={20} /></button>
        </div>

        <div className="p-6 space-y-5">
          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <label className="label">Job name <span className="text-red-500">*</span></label>
              <input className="input" value={f.job_name} onChange={e => set('job_name', e.target.value)} placeholder="Store stock daily" />
            </div>
            <div>
              <label className="label">Description</label>
              <input className="input" value={f.description} onChange={e => set('description', e.target.value)} />
            </div>
          </div>

          {/* Source + target */}
          <div className="space-y-3 pt-1">
            <div className="grid md:grid-cols-[minmax(0,1fr)_auto] gap-2 items-end">
              <div>
                <label className="label">Source view or table (DATABASE.SCHEMA.NAME) <span className="text-red-500">*</span></label>
                <input className="input font-mono" list="gd-views" value={f.source_object} onChange={e => setSource(e.target.value)}
                  placeholder="V2RETAIL.ARS_GETDATA.V_GD_STORE_STOCK" />
                <datalist id="gd-views">{moduleViews.map(o => <option key={o} value={o} />)}</datalist>
              </div>
              <button onClick={runTest} disabled={testing} className="btn-secondary h-[30px]">
                {testing ? <RefreshCw size={15} className="animate-spin" /> : <FlaskConical size={15} />} Check source
              </button>
            </div>
            {test && (
              <div className={`rounded-lg border p-3 ${test.ok ? 'border-green-200 bg-green-50/40' : 'border-red-200 bg-red-50/40'}`}>
                <CheckList checks={test.checks} />
                <div className="text-[11px] text-gray-500 mt-1">Nothing is loaded by a check.</div>
              </div>
            )}
            <div>
              <label className="label">Local table in Rep_data <span className="text-red-500">*</span></label>
              <input className="input font-mono" value={f.target_table}
                onChange={e => { setTargetTouched(true); set('target_table', e.target.value) }} placeholder="GD_SF_STORE_STOCK" />
              <div className="text-[11px] text-gray-400 mt-0.5">Always starts with GD_SF_ — added if you leave it out. Two columns are added to every load: _GD_RUN_ID and _GD_LOADED_AT (UTC).</div>
            </div>
          </div>

          {/* Load mode */}
          <div>
            <label className="label">Load mode</label>
            <div className="grid md:grid-cols-3 gap-2">
              {[
                ['replace', 'Full replace', 'Reload everything each run. The new copy replaces the old one only after its row count matches Snowflake.'],
                ['incremental', 'Incremental', 'Only rows where the watermark column ≥ the last loaded value, merged on the key columns. The first run loads everything.'],
                ['append', 'Append', 'Adds every row from each run — a snapshot history. _GD_LOADED_AT tells the runs apart.'],
              ].map(([k, t, d]) => (
                <button key={k} type="button" onClick={() => set('load_mode', k)}
                  className={`text-left rounded-lg border p-3 ${f.load_mode === k ? 'border-primary-400 bg-primary-50/60 ring-1 ring-primary-300' : 'border-gray-200 hover:bg-gray-50'}`}>
                  <div className="text-sm font-semibold text-gray-900">{t}</div>
                  <div className="text-[11px] text-gray-500 mt-0.5 leading-snug">{d}</div>
                </button>
              ))}
            </div>
          </div>

          {f.load_mode === 'replace' && (
            <label className="flex items-start gap-2 text-sm cursor-pointer -mt-2">
              <input type="checkbox" className="w-4 h-4 mt-0.5 rounded" checked={!!f.allow_empty}
                onChange={e => set('allow_empty', e.target.checked)} />
              <span>
                Allow an empty Snowflake result to empty the local table
                <span className="block text-[11px] text-gray-400">Off: if Snowflake returns 0 rows while the local table has data, the run fails and keeps the data.</span>
              </span>
            </label>
          )}

          {/* Loader */}
          <div>
            <label className="label">Loader</label>
            <div className="grid md:grid-cols-2 gap-2">
              {[
                ['bulk', 'Bulk copy', 'Default and fastest. Loads chunk files with SQL Server bulk copy (bcp), 3 at a time. If the data can’t go through it exactly, the run uses classic insert and says why.'],
                ['classic', 'Classic insert', 'Rows inserted in batches by the app — about 5–8× slower. Choose it only if bulk copy gives you trouble on this job.'],
              ].map(([k, t, d]) => (
                <button key={k} type="button" onClick={() => set('loader', k)}
                  className={`text-left rounded-lg border p-3 ${f.loader === k ? 'border-primary-400 bg-primary-50/60 ring-1 ring-primary-300' : 'border-gray-200 hover:bg-gray-50'}`}>
                  <div className="text-sm font-semibold text-gray-900">{t}{k === 'bulk' && <span className="ml-2 text-[10px] font-medium px-1.5 py-0.5 rounded bg-green-100 text-green-700">default</span>}</div>
                  <div className="text-[11px] text-gray-500 mt-0.5 leading-snug">{d}</div>
                </button>
              ))}
            </div>
          </div>

          <div className="grid md:grid-cols-2 gap-4">
            <div>
              <label className="label">Key columns {f.load_mode === 'incremental' ? <span className="text-red-500">*</span> : '(optional — indexes the table)'}</label>
              <input className="input font-mono" value={f.key_cols} onChange={e => set('key_cols', e.target.value)} placeholder="ST_CD, ARTICLE_NUMBER" />
              {cols.length > 0 && (
                <div className="flex flex-wrap gap-1 mt-1 max-h-24 overflow-y-auto">
                  {cols.map(c => (
                    <button key={c.name} type="button" onClick={() => toggleKey(c.name)}
                      className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${keyList.includes(c.name.toUpperCase()) ? 'bg-primary-100 border-primary-300 text-primary-800' : 'bg-white border-gray-200 text-gray-600'}`}>
                      {c.name}
                    </button>
                  ))}
                </div>
              )}
            </div>
            {f.load_mode === 'incremental' && (
              <div>
                <label className="label">Watermark column <span className="text-red-500">*</span></label>
                {cols.length > 0 ? (
                  <select className="input font-mono" value={f.watermark_col} onChange={e => set('watermark_col', e.target.value)}>
                    <option value="">— pick a date, timestamp or number column —</option>
                    {cols.filter(c => ['FIXED', 'DATE', 'TIMESTAMP', 'TIMESTAMP_NTZ', 'TIMESTAMP_LTZ', 'TIMESTAMP_TZ', 'TEXT'].includes(c.sf_type))
                      .map(c => <option key={c.name} value={c.name}>{c.name} · {c.sf_type_label}</option>)}
                  </select>
                ) : (
                  <input className="input font-mono" value={f.watermark_col} onChange={e => set('watermark_col', e.target.value)} placeholder="BILL_DATE" />
                )}
                <div className="text-[11px] text-gray-400 mt-0.5">
                  {f.watermark_value ? `Last loaded value: ${f.watermark_value}. ` : ''}Use “Check source” to pick from the columns.
                </div>
              </div>
            )}
          </div>

          {/* Columns — rename / skip */}
          {(f.column_map || []).length > 0 && (
            <ColumnsEditor map={f.column_map} cols={cols} issues={issues} note={mapNote}
              onChange={m => set('column_map', m)} />
          )}

          {/* Schedule */}
          <div className="pt-4 border-t space-y-3">
            <div className="flex flex-wrap items-center gap-4">
              <label className="label mb-0">Trigger</label>
              {[['schedule', 'On a schedule'], ['manual', 'Manual only (Run now)']].map(([k, l]) => (
                <label key={k} className="flex items-center gap-1.5 text-sm cursor-pointer">
                  <input type="radio" checked={f.trigger_type === k} onChange={() => set('trigger_type', k)} /> {l}
                </label>
              ))}
            </div>
            {f.trigger_type === 'schedule' && <ScheduleEditor f={f} set={set} />}
          </div>

          <div className="grid md:grid-cols-2 gap-4 pt-4 border-t">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="checkbox" className="w-4 h-4 rounded" checked={!!f.retry_on_fail} onChange={e => set('retry_on_fail', e.target.checked)} />
              If a scheduled run fails, retry once after
              <select className="input w-20 py-0.5" value={f.retry_delay_min} disabled={!f.retry_on_fail}
                onChange={e => set('retry_delay_min', Number(e.target.value))}>
                {[5, 10, 15, 30, 60].map(m => <option key={m} value={m}>{m} min</option>)}
              </select>
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="checkbox" className="w-4 h-4 rounded" checked={!!f.enabled} onChange={e => set('enabled', e.target.checked)} />
              Enabled
            </label>
          </div>

          {err && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{err}</div>}
        </div>

        <div className="flex justify-end gap-3 px-6 py-4 border-t sticky bottom-0 bg-white">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button onClick={save} disabled={saving} className="btn-primary">{saving ? 'Saving…' : 'Save job'}</button>
        </div>
      </div>
    </div>
  )
}

function ColumnsEditor({ map, cols, issues, note, onChange }) {
  const typeOf = Object.fromEntries((cols || []).map(c => [c.name.toUpperCase(), c]))
  const s = mapSummary(map)
  const update = (i, patch) => onChange(map.map((e, j) => (j === i ? { ...e, ...patch } : e)))
  return (
    <div className="pt-4 border-t space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <label className="label mb-0">Columns</label>
        {s.renamed > 0 && <span className="text-[11px] px-2 py-0.5 rounded-full bg-primary-50 text-primary-700">{s.renamed} renamed</span>}
        {s.skipped > 0 && <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">{s.skipped} skipped</span>}
        <span className="flex-1" />
        <button type="button" onClick={() => onChange(map.map(e => ({ ...e, name: e.source })))} className="btn-ghost text-xs">Reset names</button>
        <button type="button" onClick={() => onChange(map.map(e => ({ ...e, load: true })))} className="btn-ghost text-xs">Load all</button>
      </div>
      <div className="text-[11px] text-gray-500">
        Type a new name to rename a column in the local table, or untick Load to leave it out. Keys and the watermark are still picked by their Snowflake name.
        {!cols?.length && ' Click “Check source” to refresh types and see any new Snowflake columns.'}
      </div>
      {note && <div className="text-[11px] text-amber-700">{note}</div>}
      <div className="border border-gray-200 rounded-lg overflow-auto max-h-80">
        <table className="w-full text-xs">
          <thead className="bg-gray-50 sticky top-0 z-10">
            <tr>
              <th className="px-2 py-1.5 text-left">Snowflake column</th>
              <th className="px-2 py-1.5 text-left">Snowflake type</th>
              <th className="px-2 py-1.5 text-center">Load</th>
              <th className="px-2 py-1.5 text-left">Name in local table</th>
              <th className="px-2 py-1.5 text-left">Local type</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {map.map((e, i) => {
              const t = typeOf[e.source.toUpperCase()]
              const bad = issues.row[e.source.toUpperCase()]
              const renamed = e.load && e.name !== e.source
              return (
                <tr key={e.source} className={e.load ? '' : 'opacity-50'}>
                  <td className="px-2 py-1 font-mono">{e.source}</td>
                  <td className="px-2 py-1 font-mono text-gray-500">{t?.sf_type_label || '—'}</td>
                  <td className="px-2 py-1 text-center">
                    <input type="checkbox" className="w-4 h-4 rounded" checked={!!e.load} onChange={ev => update(i, { load: ev.target.checked })} />
                  </td>
                  <td className="px-2 py-1">
                    {e.load ? (
                      <input className={`input font-mono py-0.5 ${bad ? 'border-red-400' : renamed ? 'border-primary-400' : ''}`}
                        value={e.name} onChange={ev => update(i, { name: ev.target.value })} />
                    ) : <span className="text-gray-400">not loaded</span>}
                    {bad && <div className="text-[11px] text-red-600 mt-0.5">{bad}</div>}
                  </td>
                  <td className="px-2 py-1 font-mono text-gray-500">{e.load ? (t?.sql_type || '—') : '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {issues.general.map(g => <div key={g} className="text-[11px] text-red-600">{g}</div>)}
    </div>
  )
}

function ScheduleEditor({ f, set }) {
  const setTime = (i, v) => set('times', f.times.map((t, j) => (j === i ? v : t)))
  return (
    <div className="space-y-3">
      <div className="grid md:grid-cols-3 gap-3">
        <div>
          <label className="label">Frequency</label>
          <select className="input" value={f.freq} onChange={e => set('freq', e.target.value)}>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
            <option value="every_n_hours">Every N hours</option>
          </select>
        </div>
        {f.freq === 'every_n_hours' ? (
          <>
            <div>
              <label className="label">Every (hours)</label>
              <input type="number" min={1} max={24} className="input" value={f.every_n_hours} onChange={e => set('every_n_hours', e.target.value)} />
            </div>
            <div>
              <label className="label">Only between (IST, optional)</label>
              <div className="flex items-center gap-1">
                <input type="time" className="input" value={f.win_start} onChange={e => set('win_start', e.target.value)} />
                <span className="text-gray-400">–</span>
                <input type="time" className="input" value={f.win_end} onChange={e => set('win_end', e.target.value)} />
              </div>
            </div>
          </>
        ) : (
          <div className="md:col-span-2">
            <label className="label">Run at (IST)</label>
            <div className="flex flex-wrap items-center gap-2">
              {f.times.map((t, i) => (
                <span key={i} className="inline-flex items-center gap-1">
                  <input type="time" className="input w-28" value={t} onChange={e => setTime(i, e.target.value)} />
                  {f.times.length > 1 && (
                    <button type="button" onClick={() => set('times', f.times.filter((_, j) => j !== i))} className="text-gray-400 hover:text-red-500"><X size={14} /></button>
                  )}
                </span>
              ))}
              <button type="button" onClick={() => set('times', [...f.times, '13:00'])} className="text-xs text-primary-600 inline-flex items-center gap-1"><Plus size={13} /> Add time</button>
            </div>
          </div>
        )}
      </div>
      {f.freq === 'weekly' && (
        <div>
          <label className="label">On</label>
          <div className="flex flex-wrap gap-1">
            {WD.map((d, i) => {
              const on = f.weekdays.includes(i)
              return (
                <button key={d} type="button"
                  onClick={() => set('weekdays', on ? f.weekdays.filter(w => w !== i) : [...f.weekdays, i].sort())}
                  className={`px-2.5 py-1 text-xs rounded border ${on ? 'bg-primary-50 border-primary-400 text-primary-700' : 'bg-white border-gray-300 text-gray-600'}`}>{d}</button>
              )
            })}
          </div>
        </div>
      )}
      {f.freq === 'monthly' && (
        <div className="w-64">
          <label className="label">Days of the month</label>
          <input className="input" value={f.days} onChange={e => set('days', e.target.value)} placeholder="1, 15" />
          <div className="text-[11px] text-gray-400 mt-0.5">31 runs on the last day in shorter months.</div>
        </div>
      )}
      <div className="text-[11px] text-gray-500 flex items-center gap-1"><Clock size={12} /> A run missed while the server was down starts once when it's back.</div>
    </div>
  )
}

function DeleteDialog({ job, onClose, onDone }) {
  const [dropTable, setDropTable] = useState(false)
  const [busy, setBusy] = useState(false)
  const go = async () => {
    setBusy(true)
    try {
      const { data } = await getDataAPI.deleteJob(job.JOB_ID, dropTable)
      toast.success(data.message || 'Deleted')
      onDone()
    } catch (e) {
      toast.error(errText(e, 'Delete failed'))
    } finally { setBusy(false) }
  }
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-md p-6 space-y-4" onClick={e => e.stopPropagation()}>
        <h3 className="font-semibold text-gray-900">Delete “{job.JOB_NAME}”?</h3>
        <p className="text-sm text-gray-600">The job and its schedule are removed. Its run history stays.</p>
        <label className="flex items-start gap-2 text-sm cursor-pointer">
          <input type="checkbox" className="w-4 h-4 mt-0.5 rounded" checked={dropTable} onChange={e => setDropTable(e.target.checked)} />
          <span>Also drop the local table <span className="font-mono">{job.TARGET_TABLE}</span>. This can't be undone.</span>
        </label>
        <div className="flex justify-end gap-3">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button onClick={go} disabled={busy} className="btn-danger">{busy ? 'Deleting…' : 'Delete job'}</button>
        </div>
      </div>
    </div>
  )
}
