import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Snowflake, Plus, RefreshCw, ListChecks, Eye, Check, Trash2, Folder, Search, Table2,
  ArrowRight, Copy, ChevronDown, ChevronRight,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { getDataAPI } from '@/services/api'
import {
  fmtNum, fmtIST, SnowflakeBanner, PreviewGrid, CheckList, errText, useGetDataPerms,
} from '@/components/getdata/gdUtils'

const DB = 'V2RETAIL'
const SCHEMA = 'ARS_GETDATA'
const PREFIX = 'V_GD_'
const blankEditor = { isNew: true, name: '', description: '', sql: '', managed: false, versions: [], jobs: [] }

export default function GetDataViewsPage() {
  const navigate = useNavigate()
  const { canManage, canRun } = useGetDataPerms()
  const [status, setStatus] = useState(null)
  const [views, setViews] = useState({ items: [] })
  const [loadingList, setLoadingList] = useState(true)
  const [leftTab, setLeftTab] = useState('module')
  const [mode, setMode] = useState('edit')            // edit | object
  const [ed, setEd] = useState(blankEditor)
  const [obj, setObj] = useState(null)                // { object, type, row_count, columns }
  const [validation, setValidation] = useState(null)
  const [validatedSql, setValidatedSql] = useState(null)
  const [preview, setPreview] = useState(null)
  const [previewErr, setPreviewErr] = useState(null)
  const [busy, setBusy] = useState('')                // validate | preview | save | load | drop

  const loadViews = async () => {
    setLoadingList(true)
    try {
      const { data } = await getDataAPI.listViews()
      setViews(data.data)
    } catch (e) {
      toast.error(errText(e, 'Could not list views'))
    } finally { setLoadingList(false) }
  }

  useEffect(() => {
    getDataAPI.sfStatus().then(({ data }) => setStatus(data.data)).catch(() => {})
    loadViews()
  }, [])

  const resetResults = () => { setValidation(null); setValidatedSql(null); setPreview(null); setPreviewErr(null) }

  const newView = () => { setMode('edit'); setEd(blankEditor); resetResults() }

  const openView = async (name) => {
    setMode('edit'); resetResults(); setBusy('load')
    try {
      const { data } = await getDataAPI.getView(name)
      const v = data.data
      setEd({ isNew: false, name: v.name, description: v.description || '', sql: v.sql || '',
              managed: v.managed, versions: v.versions || [], jobs: v.jobs || [], note: v.note,
              version: v.version, updated_by: v.updated_by, updated_at: v.updated_at })
    } catch (e) {
      toast.error(errText(e, 'Could not open the view'))
    } finally { setBusy('') }
  }

  const openObject = async (o) => {
    setMode('object'); resetResults(); setObj({ ...o, columns: null }); setBusy('load')
    try {
      const { data } = await getDataAPI.sfColumns(o.object)
      setObj({ ...o, columns: data.data.columns })
    } catch (e) {
      setObj({ ...o, columns: [], error: errText(e) })
    } finally { setBusy('') }
  }

  const sqlChanged = validatedSql !== null && validatedSql !== ed.sql
  const canSave = validation?.ok && !sqlChanged && ed.sql.trim()

  const validate = async () => {
    if (!ed.sql.trim()) { toast.error('Write a SELECT query first'); return }
    setBusy('validate'); setValidation(null)
    try {
      const { data } = await getDataAPI.validate(ed.sql)
      setValidation(data.data); setValidatedSql(ed.sql)
    } catch (e) {
      setValidation({ ok: false, checks: [{ label: 'Validation', ok: false, detail: errText(e) }] })
      setValidatedSql(ed.sql)
    } finally { setBusy('') }
  }

  const runPreview = async (body) => {
    setBusy('preview'); setPreview(null); setPreviewErr(null)
    try {
      const { data } = await getDataAPI.preview({ ...body, limit: 100 })
      setPreview(data.data)
    } catch (e) {
      setPreviewErr(errText(e, 'Preview failed'))
    } finally { setBusy('') }
  }

  const save = async () => {
    if (!ed.name.trim()) { toast.error('Give the view a name'); return }
    setBusy('save')
    try {
      const { data } = await getDataAPI.saveView({ name: ed.name, sql: ed.sql, description: ed.description })
      const res = data.data
      setValidation(res.validation); setValidatedSql(ed.sql)
      if (res.created) {
        toast.success(`Saved ${res.object}`)
        await loadViews()
        await openView(res.name)
      } else {
        toast.error('Validation failed — nothing was created')
      }
    } catch (e) {
      toast.error(errText(e, 'Save failed'))
    } finally { setBusy('') }
  }

  const drop = async () => {
    if (!confirm(`Drop ${DB}.${SCHEMA}.${ed.name} in Snowflake? Its SQL history stays in the app.`)) return
    setBusy('drop')
    try {
      await getDataAPI.dropView(ed.name)
      toast.success(`Dropped ${ed.name}`)
      newView(); loadViews()
    } catch (e) {
      toast.error(errText(e, 'Drop failed'))
    } finally { setBusy('') }
  }

  const displayName = ed.name
    ? (ed.name.toUpperCase().startsWith(PREFIX) ? ed.name.toUpperCase() : PREFIX + ed.name.toUpperCase())
    : ''

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><Snowflake size={24} /> Snowflake Views</h1>
          <p className="text-gray-500 text-sm mt-0.5">
            Write a SELECT, validate it, preview it, then create it as a view in <span className="font-mono">{DB}.{SCHEMA}</span>. Sync jobs copy views into local SQL.
          </p>
        </div>
        {canManage && <button onClick={newView} className="btn-primary"><Plus size={16} /> New view</button>}
      </div>

      <SnowflakeBanner status={status} />

      <div className="grid lg:grid-cols-[290px_minmax(0,1fr)] gap-4 items-start">
        {/* Left: module views + browser */}
        <div className="card p-0 overflow-hidden">
          <div className="flex border-b text-xs font-semibold">
            {[['module', `Module views (${views.items?.length || 0})`], ['browse', 'Browse Snowflake']].map(([k, l]) => (
              <button key={k} onClick={() => setLeftTab(k)}
                className={`flex-1 px-3 py-2 ${leftTab === k ? 'text-primary-700 border-b-2 border-primary-500 bg-primary-50/50' : 'text-gray-500 hover:bg-gray-50'}`}>{l}</button>
            ))}
          </div>
          {leftTab === 'module'
            ? <ModuleViewList views={views} loading={loadingList} onReload={loadViews}
                active={mode === 'edit' && !ed.isNew ? ed.name : null} onOpen={openView} />
            : <Browser onOpen={openObject} activeObject={mode === 'object' ? obj?.object : null} />}
        </div>

        {/* Right: editor or object */}
        {mode === 'edit' ? (
          <div className="card p-4 space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="font-semibold text-gray-900">{ed.isNew ? 'New view' : ed.name}</div>
                {!ed.isNew && (
                  <div className="text-xs text-gray-500 mt-0.5">
                    {ed.managed ? <>Version {ed.version} · saved by {ed.updated_by || '—'} · {fmtIST(ed.updated_at)}</> : ed.note}
                  </div>
                )}
              </div>
              {busy === 'load' && <RefreshCw size={16} className="animate-spin text-primary-600" />}
            </div>

            <div className="grid md:grid-cols-2 gap-3">
              <div>
                <label className="label">View name</label>
                <input className="input font-mono" value={ed.name} disabled={!ed.isNew || !canManage}
                  onChange={e => setEd(p => ({ ...p, name: e.target.value }))} placeholder="STORE_STOCK" />
                {ed.isNew && displayName && <div className="text-[11px] text-gray-400 mt-0.5">Created as <span className="font-mono">{displayName}</span></div>}
              </div>
              <div>
                <label className="label">Created in (fixed)</label>
                <input className="input font-mono bg-gray-50" value={`${DB}.${SCHEMA}`} readOnly />
              </div>
              <div className="md:col-span-2">
                <label className="label">Description</label>
                <input className="input" value={ed.description} disabled={!canManage}
                  onChange={e => setEd(p => ({ ...p, description: e.target.value }))} placeholder="Store × article stock by SLOC, non-zero only" />
              </div>
            </div>

            <div>
              <label className="label">SELECT query (SELECT or WITH only)</label>
              <textarea className="input font-mono text-xs h-56 leading-relaxed" spellCheck={false}
                value={ed.sql} readOnly={!canManage}
                onChange={e => setEd(p => ({ ...p, sql: e.target.value }))}
                placeholder={'SELECT WERKS AS ST_CD, MATNR AS ARTICLE_NUMBER, LGORT AS SLOC, SUM(LABST) AS STK_QTY\nFROM V2RETAIL.BRONZE.SAP_MARD\nWHERE LABST <> 0\nGROUP BY 1, 2, 3'} />
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {canRun && <button onClick={validate} disabled={!!busy} className="btn-secondary">
                {busy === 'validate' ? <RefreshCw size={15} className="animate-spin" /> : <ListChecks size={15} />} Validate</button>}
              {canRun && <button onClick={() => runPreview({ sql: ed.sql })} disabled={!!busy || !ed.sql.trim()} className="btn-secondary">
                {busy === 'preview' ? <RefreshCw size={15} className="animate-spin" /> : <Eye size={15} />} Preview 100 rows</button>}
              {!ed.isNew && ed.managed && (
                <button onClick={() => navigate(`/get-data/snowflake/jobs?source=${encodeURIComponent(`${DB}.${SCHEMA}.${ed.name}`)}`)} className="btn-secondary">
                  <ArrowRight size={15} /> Create sync job</button>
              )}
              <div className="flex-1" />
              {canManage && !ed.isNew && (
                <button onClick={drop} disabled={!!busy} className="btn-ghost text-red-600" title="Drop the view in Snowflake">
                  <Trash2 size={15} /> Drop</button>
              )}
              {canManage && (
                <button onClick={save} disabled={!!busy || !canSave} className="btn-primary"
                  title={canSave ? '' : 'Validate the current SQL first'}>
                  {busy === 'save' ? <RefreshCw size={15} className="animate-spin" /> : <Check size={15} />}
                  {ed.isNew ? 'Create view' : 'Save new version'}
                </button>
              )}
            </div>
            {canManage && !canSave && ed.sql.trim() && (
              <div className="text-xs text-gray-500 -mt-2">
                {sqlChanged ? 'The SQL changed since it was validated — validate again to save.'
                  : validation && !validation.ok ? 'Fix the failed checks, then validate again.'
                  : 'Validate the query to enable saving.'}
              </div>
            )}

            {validation && (
              <div className={`rounded-lg border p-3 ${validation.ok ? 'border-green-200 bg-green-50/40' : 'border-red-200 bg-red-50/40'}`}>
                <CheckList checks={validation.checks} />
                {validation.columns?.length > 0 && <ColumnTable columns={validation.columns} />}
              </div>
            )}

            {previewErr && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">{previewErr}</div>}
            {preview && <PreviewGrid data={preview} />}

            {!ed.isNew && ed.jobs?.length > 0 && (
              <div className="text-xs text-gray-600">
                <span className="font-semibold">Used by:</span>{' '}
                {ed.jobs.map(j => j.job_name).join(', ')}
              </div>
            )}
            {!ed.isNew && ed.versions?.length > 0 && <Versions versions={ed.versions} />}
          </div>
        ) : (
          <ObjectPanel obj={obj} busy={busy} preview={preview} previewErr={previewErr} canRun={canRun} canManage={canManage}
            onPreview={() => runPreview({ object: obj.object })}
            onJob={() => navigate(`/get-data/snowflake/jobs?source=${encodeURIComponent(obj.object)}`)}
            onCopy={() => {
              setMode('edit'); resetResults()
              setEd({ ...blankEditor, sql: `SELECT *\nFROM ${obj.object}` })
            }} />
        )}
      </div>
    </div>
  )
}

function ModuleViewList({ views, loading, onReload, active, onOpen }) {
  return (
    <div>
      <div className="flex items-center justify-between px-3 py-2 text-[11px] text-gray-500">
        <span className="font-mono">{DB}.{SCHEMA}</span>
        <button onClick={onReload} className="p-1 hover:bg-gray-100 rounded" title="Refresh"><RefreshCw size={13} className={loading ? 'animate-spin' : ''} /></button>
      </div>
      {views.snowflake_error && (
        <div className="mx-3 mb-2 text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded p-2">
          Snowflake unreachable — showing the app's saved copies. {views.snowflake_error}
        </div>
      )}
      {!loading && (views.items || []).length === 0 && (
        <div className="px-3 pb-4 text-xs text-gray-400">No views yet. Create one with “New view”.</div>
      )}
      <ul className="max-h-[60vh] overflow-y-auto">
        {(views.items || []).map(v => (
          <li key={v.name}>
            <button onClick={() => onOpen(v.name)}
              className={`w-full text-left px-3 py-2 border-l-2 ${active === v.name ? 'border-primary-500 bg-primary-50' : 'border-transparent hover:bg-gray-50'}`}>
              <div className="font-mono text-xs text-gray-900 truncate">{v.name}</div>
              <div className="flex flex-wrap gap-1 mt-0.5 text-[10px] text-gray-500">
                {v.version ? <span>v{v.version}</span> : null}
                {v.column_count ? <span>· {v.column_count} cols</span> : null}
                {v.jobs?.length ? <span className="text-primary-700">· {v.jobs.length} job(s)</span> : null}
                {v.missing && <span className="px-1 rounded bg-red-100 text-red-700">missing in Snowflake</span>}
                {v.in_snowflake && !v.managed && <span className="px-1 rounded bg-gray-100">made outside app</span>}
              </div>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function Browser({ onOpen, activeObject }) {
  const [schemas, setSchemas] = useState(null)
  const [schema, setSchema] = useState(null)
  const [objects, setObjects] = useState([])
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    setLoading(true)
    getDataAPI.sfSchemas()
      .then(({ data }) => setSchemas(data.data.items))
      .catch(e => setErr(errText(e, 'Could not list schemas')))
      .finally(() => setLoading(false))
  }, [])

  const pick = async (s) => {
    setSchema(s); setObjects([]); setFilter(''); setLoading(true); setErr(null)
    try {
      const { data } = await getDataAPI.sfObjects({ schema: s })
      setObjects(data.data.items)
    } catch (e) {
      setErr(errText(e))
    } finally { setLoading(false) }
  }

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase()
    return f ? objects.filter(o => o.name.toLowerCase().includes(f)) : objects
  }, [objects, filter])

  if (err) return <div className="p-3 text-xs text-red-600">{err}</div>
  if (!schema) {
    return (
      <ul className="max-h-[65vh] overflow-y-auto">
        {loading && <li className="p-3 text-xs text-gray-400 flex items-center gap-2"><RefreshCw size={12} className="animate-spin" /> Loading schemas…</li>}
        {(schemas || []).map(s => (
          <li key={s.schema}>
            <button onClick={() => pick(s.schema)} className="w-full text-left px-3 py-1.5 hover:bg-gray-50 flex items-center justify-between text-xs">
              <span className="flex items-center gap-1.5"><Folder size={13} className="text-gray-400" /> {s.schema}</span>
              <span className="text-gray-400">{s.views} views · {s.tables} tables</span>
            </button>
          </li>
        ))}
      </ul>
    )
  }
  return (
    <div>
      <div className="px-3 py-2 flex items-center gap-2 border-b">
        <button onClick={() => setSchema(null)} className="text-xs text-primary-600">← Schemas</button>
        <span className="text-xs font-mono text-gray-700">{schema}</span>
      </div>
      <div className="p-2 relative">
        <Search size={13} className="absolute left-4 top-1/2 -translate-y-1/2 text-gray-400" />
        <input className="input pl-7" value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter by name" />
      </div>
      <ul className="max-h-[55vh] overflow-y-auto">
        {loading && <li className="p-3 text-xs text-gray-400 flex items-center gap-2"><RefreshCw size={12} className="animate-spin" /> Loading…</li>}
        {shown.map(o => (
          <li key={o.object}>
            <button onClick={() => onOpen(o)}
              className={`w-full text-left px-3 py-1.5 text-xs border-l-2 ${activeObject === o.object ? 'border-primary-500 bg-primary-50' : 'border-transparent hover:bg-gray-50'}`}>
              <div className="flex items-center gap-1.5">
                {o.type === 'view' ? <Eye size={12} className="text-gray-400" /> : <Table2 size={12} className="text-gray-400" />}
                <span className="font-mono truncate">{o.name}</span>
              </div>
              {o.type === 'table' && o.row_count != null && <div className="text-[10px] text-gray-400 ml-5">{fmtNum(o.row_count)} rows</div>}
            </button>
          </li>
        ))}
        {!loading && shown.length === 0 && <li className="p-3 text-xs text-gray-400">Nothing matches.</li>}
      </ul>
    </div>
  )
}

function ObjectPanel({ obj, busy, preview, previewErr, canRun, canManage, onPreview, onJob, onCopy }) {
  if (!obj) return null
  return (
    <div className="card p-4 space-y-4">
      <div>
        <div className="font-mono font-semibold text-gray-900 break-all">{obj.object}</div>
        <div className="text-xs text-gray-500 mt-0.5">
          {obj.type === 'view' ? 'View' : 'Table'}{obj.row_count != null ? ` · ${fmtNum(obj.row_count)} rows` : ''} · read-only source
          {obj.comment ? ` · ${obj.comment}` : ''}
        </div>
      </div>
      <div className="flex flex-wrap gap-2">
        {canRun && <button onClick={onPreview} disabled={!!busy} className="btn-secondary">
          {busy === 'preview' ? <RefreshCw size={15} className="animate-spin" /> : <Eye size={15} />} Preview 100 rows</button>}
        {canManage && <button onClick={onJob} className="btn-secondary"><ArrowRight size={15} /> Create sync job</button>}
        {canManage && <button onClick={onCopy} className="btn-secondary"><Copy size={15} /> Start a view from this</button>}
      </div>
      {obj.error && <div className="text-sm text-red-600">{obj.error}</div>}
      {busy === 'load' && <div className="text-xs text-gray-400 flex items-center gap-2"><RefreshCw size={12} className="animate-spin" /> Reading columns…</div>}
      {obj.columns?.length > 0 && <ColumnTable columns={obj.columns} open />}
      {previewErr && <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">{previewErr}</div>}
      {preview && <PreviewGrid data={preview} />}
    </div>
  )
}

function ColumnTable({ columns, open: initiallyOpen = false }) {
  const [open, setOpen] = useState(initiallyOpen)
  return (
    <div className="mt-2">
      <button onClick={() => setOpen(o => !o)} className="text-xs text-gray-600 inline-flex items-center gap-1 font-medium">
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />} {columns.length} columns · Snowflake type → local SQL type
      </button>
      {open && (
        <div className="mt-1 max-h-64 overflow-auto border border-gray-200 rounded">
          <table className="w-full text-xs">
            <thead className="bg-gray-50 sticky top-0"><tr>
              <th className="px-2 py-1 text-left">Column</th><th className="px-2 py-1 text-left">Snowflake</th><th className="px-2 py-1 text-left">Local SQL</th>
            </tr></thead>
            <tbody className="divide-y divide-gray-100">
              {columns.map(c => (
                <tr key={c.name}>
                  <td className="px-2 py-1 font-mono">{c.name}</td>
                  <td className="px-2 py-1 font-mono text-gray-500">{c.sf_type_label}</td>
                  <td className="px-2 py-1 font-mono text-gray-700">{c.sql_type}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Versions({ versions }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button onClick={() => setOpen(o => !o)} className="text-xs text-gray-600 inline-flex items-center gap-1 font-medium">
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />} History ({versions.length})
      </button>
      {open && (
        <table className="w-full text-xs mt-1">
          <thead className="bg-gray-50"><tr>
            <th className="px-2 py-1 text-left">Version</th><th className="px-2 py-1 text-left">Action</th>
            <th className="px-2 py-1 text-right">Columns</th><th className="px-2 py-1 text-right">Rows</th>
            <th className="px-2 py-1 text-left">By</th><th className="px-2 py-1 text-left">When (IST)</th>
          </tr></thead>
          <tbody className="divide-y divide-gray-100">
            {versions.map((v, i) => (
              <tr key={i}>
                <td className="px-2 py-1">v{v.VERSION}</td>
                <td className="px-2 py-1">{v.ACTION === 'dropped' ? <span className="text-red-600">dropped</span> : v.ACTION}</td>
                <td className="px-2 py-1 text-right">{fmtNum(v.COLUMN_COUNT)}</td>
                <td className="px-2 py-1 text-right">{fmtNum(v.ROW_COUNT)}</td>
                <td className="px-2 py-1">{v.CHANGED_BY}</td>
                <td className="px-2 py-1">{fmtIST(v.CHANGED_AT)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
