import { useEffect, useState } from 'react'
import { FileDown, Plus, Play, Pencil, Trash2, RefreshCw, X, Clock, CheckCircle, XCircle, Power } from 'lucide-react'
import { sapAPI } from '@/services/api'
import useAuthStore from '@/store/authStore'
import toast from 'react-hot-toast'
import WhereBuilder from '@/components/sap/WhereBuilder'
import { cleanConds } from '@/utils/sapWhere'

const blank = {
  name: '', description: '', door: 'rfc_table',
  sap_table: '', fields: '', where_conds: [], where_raw: '',
  odata_service: '', odata_entity: '', odata_filter: '', odata_select: '',
  sf_query: '', sf_database: 'V2RETAIL', sf_schema: 'BRONZE',
  env: '', row_limit: 50000, target_table: '', write_mode: 'replace',
  trigger_type: 'manual', sched_freq: 'daily', sched_time: '02:00', sched_hours: 6,
  enabled: true,
}

const statusPill = (s) => {
  const v = String(s || '').toLowerCase()
  if (v === 'success') return 'bg-green-100 text-green-700'
  if (v === 'failed') return 'bg-red-100 text-red-700'
  if (v === 'running') return 'bg-amber-100 text-amber-700'
  return 'bg-gray-100 text-gray-500'
}

export default function SapPullsPage() {
  const isAdmin = useAuthStore(s => s.isSuperAdmin?.() || s.hasRole?.('ADMIN'))
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState(null)   // form object or null
  const [saving, setSaving] = useState(false)
  const [runningId, setRunningId] = useState(null)

  useEffect(() => { load() }, [])

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await sapAPI.listPulls()
      setItems(data?.data?.items || [])
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load pulls')
    } finally { setLoading(false) }
  }

  const openNew = () => setEditing({ ...blank })

  const openEdit = (p) => {
    const sc = p.SCHEDULE_CONFIG ? safeParse(p.SCHEDULE_CONFIG) : {}
    setEditing({
      pull_id: p.PULL_ID,
      name: p.NAME || '', description: p.DESCRIPTION || '', door: p.DOOR || 'rfc_table',
      sap_table: p.SAP_TABLE || '',
      fields: Array.isArray(p.FIELDS) ? p.FIELDS.join(',') : (p.FIELDS || ''),
      where_conds: Array.isArray(p.WHERE_JSON) ? p.WHERE_JSON : [],
      where_raw: (Array.isArray(p.WHERE_JSON) && p.WHERE_JSON.length) ? '' : (p.WHERE_CLAUSE || ''),
      odata_service: p.ODATA_SERVICE || '', odata_entity: p.ODATA_ENTITY || '',
      odata_filter: p.ODATA_FILTER || '', odata_select: p.ODATA_SELECT || '',
      sf_query: p.SF_QUERY || '', sf_database: p.SF_DATABASE || 'V2RETAIL', sf_schema: p.SF_SCHEMA || 'BRONZE',
      env: p.ENV || '', row_limit: p.ROW_LIMIT || 50000,
      target_table: p.TARGET_TABLE || '', write_mode: p.WRITE_MODE || 'replace',
      trigger_type: p.TRIGGER_TYPE || 'manual',
      sched_freq: sc.freq || 'daily',
      sched_time: (sc.times && sc.times[0]) || sc.time || '02:00',
      sched_hours: sc.every_n_hours || 6,
      enabled: !!p.ENABLED,
    })
  }

  const set = (k, v) => setEditing(p => ({ ...p, [k]: v }))

  const buildPayload = (f) => {
    let schedule_config = null
    if (f.trigger_type === 'schedule') {
      schedule_config = f.sched_freq === 'every_n_hours'
        ? { freq: 'every_n_hours', every_n_hours: Number(f.sched_hours) || 6 }
        : { freq: f.sched_freq, times: [f.sched_time || '02:00'] }
    }
    return {
      name: f.name, description: f.description, door: f.door,
      sap_table: f.sap_table, fields: f.fields,
      where_json: cleanConds(f.where_conds || []), where_clause: f.where_raw || '',
      odata_service: f.odata_service, odata_entity: f.odata_entity,
      odata_filter: f.odata_filter, odata_select: f.odata_select,
      sf_query: f.sf_query, sf_database: f.sf_database, sf_schema: f.sf_schema,
      env: f.env || null, row_limit: Number(f.row_limit) || 50000,
      target_table: f.target_table, write_mode: f.write_mode,
      trigger_type: f.trigger_type, schedule_config, enabled: !!f.enabled,
    }
  }

  const save = async () => {
    const f = editing
    if (!f.name?.trim() || !f.target_table?.trim()) { toast.error('Name and Target table are required'); return }
    setSaving(true)
    try {
      const payload = buildPayload(f)
      if (f.pull_id) await sapAPI.updatePull(f.pull_id, payload)
      else await sapAPI.createPull(payload)
      toast.success('Saved')
      setEditing(null); load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Save failed')
    } finally { setSaving(false) }
  }

  const runNow = async (p) => {
    setRunningId(p.PULL_ID)
    try {
      const { data } = await sapAPI.runPull(p.PULL_ID)
      if (data?.success) toast.success(data.message || 'Pull completed')
      else toast.error(data?.message || 'Pull failed')
      load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Run failed')
    } finally { setRunningId(null) }
  }

  const toggle = async (p) => {
    try { await sapAPI.enablePull(p.PULL_ID, !p.ENABLED); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Failed') }
  }

  const remove = async (p) => {
    if (!confirm(`Delete pull "${p.NAME}"? (The landed SAP_ table is NOT dropped.)`)) return
    try { await sapAPI.deletePull(p.PULL_ID); toast.success('Deleted'); load() }
    catch (e) { toast.error(e.response?.data?.detail || 'Delete failed') }
  }

  if (loading) return <div className="flex items-center justify-center h-64"><RefreshCw className="animate-spin text-primary-600" size={32} /></div>

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><FileDown size={24} /> SAP Data Pulls</h1>
          <p className="text-gray-500 text-sm mt-0.5">Define what to fetch from SAP, where to land it, and on what schedule.</p>
        </div>
        {isAdmin && <button onClick={openNew} className="btn-primary"><Plus size={16} /> New Pull</button>}
      </div>

      {items.length === 0 ? (
        <div className="card p-12 text-center text-gray-500">
          <FileDown size={32} className="mx-auto mb-2 text-gray-300" />
          No pulls yet. {isAdmin && 'Click “New Pull” to create one.'}
        </div>
      ) : (
        <div className="card p-0 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-600 text-xs uppercase">
                <tr>
                  <th className="px-4 py-2 text-left">Name</th>
                  <th className="px-4 py-2 text-left">Source</th>
                  <th className="px-4 py-2 text-left">Target</th>
                  <th className="px-4 py-2 text-left">Trigger</th>
                  <th className="px-4 py-2 text-left">Last run</th>
                  <th className="px-4 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.map(p => (
                  <tr key={p.PULL_ID} className="hover:bg-gray-50">
                    <td className="px-4 py-2">
                      <div className="font-medium text-gray-900 flex items-center gap-2">
                        {p.NAME}
                        {!p.ENABLED && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">disabled</span>}
                      </div>
                      {p.DESCRIPTION && <div className="text-xs text-gray-400">{p.DESCRIPTION}</div>}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs">
                      {p.DOOR === 'odata' ? `${p.ODATA_SERVICE}/${p.ODATA_ENTITY}`
                        : p.DOOR === 'snowflake' ? 'Snowflake' : p.SAP_TABLE}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs">{p.TARGET_TABLE}<span className="text-gray-400"> · {p.WRITE_MODE}</span></td>
                    <td className="px-4 py-2 text-xs">
                      {p.TRIGGER_TYPE === 'schedule'
                        ? <span className="inline-flex items-center gap-1 text-gray-600"><Clock size={12} /> scheduled</span>
                        : <span className="text-gray-500">manual</span>}
                      {p.NEXT_RUN_AT && p.TRIGGER_TYPE === 'schedule' && <div className="text-gray-400">next {new Date(p.NEXT_RUN_AT).toLocaleString()}</div>}
                    </td>
                    <td className="px-4 py-2">
                      {p.LAST_STATUS
                        ? <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded ${statusPill(p.LAST_STATUS)}`}>
                            {p.LAST_STATUS === 'success' ? <CheckCircle size={11} /> : p.LAST_STATUS === 'failed' ? <XCircle size={11} /> : null}
                            {p.LAST_STATUS}{p.LAST_ROWS != null ? ` · ${p.LAST_ROWS}` : ''}
                          </span>
                        : <span className="text-xs text-gray-400">—</span>}
                    </td>
                    <td className="px-4 py-2">
                      <div className="flex items-center justify-end gap-1">
                        {isAdmin && <>
                          <button onClick={() => runNow(p)} disabled={runningId === p.PULL_ID} title="Run now"
                            className="p-1.5 text-primary-600 hover:bg-primary-50 rounded">
                            {runningId === p.PULL_ID ? <RefreshCw size={15} className="animate-spin" /> : <Play size={15} />}
                          </button>
                          <button onClick={() => toggle(p)} title={p.ENABLED ? 'Disable' : 'Enable'}
                            className={`p-1.5 rounded hover:bg-gray-100 ${p.ENABLED ? 'text-green-600' : 'text-gray-400'}`}><Power size={15} /></button>
                          <button onClick={() => openEdit(p)} title="Edit" className="p-1.5 text-gray-500 hover:bg-gray-100 rounded"><Pencil size={15} /></button>
                          <button onClick={() => remove(p)} title="Delete" className="p-1.5 text-red-500 hover:bg-red-50 rounded"><Trash2 size={15} /></button>
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

      {editing && (
        <PullModal f={editing} set={set} onClose={() => setEditing(null)} onSave={save} saving={saving} />
      )}
    </div>
  )
}

function safeParse(s) { try { return JSON.parse(s) } catch { return {} } }

function PullModal({ f, set, onClose, onSave, saving }) {
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4 border-b sticky top-0 bg-white">
          <h3 className="font-semibold text-gray-900">{f.pull_id ? 'Edit Pull' : 'New Pull'}</h3>
          <button onClick={onClose} className="p-1 text-gray-400 hover:text-gray-700"><X size={20} /></button>
        </div>
        <div className="p-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="col-span-2">
              <label className="label">Name <span className="text-red-500">*</span></label>
              <input className="input" value={f.name} onChange={e => set('name', e.target.value)} placeholder="Store stock (LQUA) nightly" />
            </div>
            <div className="col-span-2">
              <label className="label">Description</label>
              <input className="input" value={f.description} onChange={e => set('description', e.target.value)} />
            </div>
          </div>

          {/* Door */}
          <div>
            <label className="label">Source door</label>
            <div className="flex gap-2">
              {[['rfc_table', 'RFC table'], ['odata', 'OData'], ['snowflake', 'Snowflake']].map(([d, lbl]) => (
                <button key={d} type="button" onClick={() => set('door', d)}
                  className={`px-3 py-1.5 text-xs font-semibold rounded border ${f.door === d ? 'bg-primary-50 text-primary-700 border-primary-400' : 'bg-white text-gray-600 border-gray-300'}`}>
                  {lbl}
                </button>
              ))}
            </div>
          </div>

          {f.door === 'rfc_table' && (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">SAP table <span className="text-red-500">*</span></label>
                <input className="input font-mono" value={f.sap_table} onChange={e => set('sap_table', e.target.value)} placeholder="LQUA" />
              </div>
              <div>
                <label className="label">Environment (blank = default)</label>
                <input className="input" value={f.env} onChange={e => set('env', e.target.value)} placeholder="prod" />
              </div>
              <div className="col-span-2">
                <label className="label">Fields (comma-separated — required for wide tables)</label>
                <input className="input font-mono" value={f.fields} onChange={e => set('fields', e.target.value)} placeholder="LGNUM,MATNR,WERKS,GESME,VERME" />
              </div>
              <div className="col-span-2">
                <label className="label">Conditions (optional)</label>
                <WhereBuilder conds={f.where_conds} onChange={v => set('where_conds', v)} listId="pull-field-list" />
                {(!f.where_conds || f.where_conds.length === 0) && (
                  <div className="mt-2">
                    <label className="label text-gray-400">Advanced — raw WHERE (used only if no conditions above)</label>
                    <input className="input font-mono" value={f.where_raw} onChange={e => set('where_raw', e.target.value)} placeholder="WERKS = 'DH24' AND LABST > 0" />
                  </div>
                )}
              </div>
            </div>
          )}
          {f.door === 'odata' && (
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">OData service <span className="text-red-500">*</span></label>
                <input className="input font-mono" value={f.odata_service} onChange={e => set('odata_service', e.target.value)} placeholder="Z_SB_ARTICLE" />
              </div>
              <div>
                <label className="label">Entity <span className="text-red-500">*</span></label>
                <input className="input font-mono" value={f.odata_entity} onChange={e => set('odata_entity', e.target.value)} placeholder="ArtMin" />
              </div>
              <div>
                <label className="label">$filter</label>
                <input className="input font-mono" value={f.odata_filter} onChange={e => set('odata_filter', e.target.value)} />
              </div>
              <div>
                <label className="label">$select</label>
                <input className="input font-mono" value={f.odata_select} onChange={e => set('odata_select', e.target.value)} />
              </div>
            </div>
          )}
          {f.door === 'snowflake' && (
            <div className="grid grid-cols-2 gap-4">
              <div className="col-span-2">
                <label className="label">SELECT query <span className="text-red-500">*</span></label>
                <textarea className="input font-mono h-28" value={f.sf_query} onChange={e => set('sf_query', e.target.value)}
                  placeholder="SELECT LGNUM,MATNR,WERKS,GESME,VERME FROM V2RETAIL.BRONZE.SAP_LQUA" />
              </div>
              <div>
                <label className="label">Database (USE, optional)</label>
                <input className="input font-mono" value={f.sf_database} onChange={e => set('sf_database', e.target.value)} placeholder="V2RETAIL" />
              </div>
              <div>
                <label className="label">Schema (USE, optional)</label>
                <input className="input font-mono" value={f.sf_schema} onChange={e => set('sf_schema', e.target.value)} placeholder="BRONZE" />
              </div>
              <div className="col-span-2 text-xs text-gray-500 bg-blue-50 border border-blue-200 rounded p-2">
                Reads directly from Snowflake with your key-pair — read-only SELECT/WITH only. The row limit below caps how many rows land.
              </div>
            </div>
          )}

          {/* Target */}
          <div className="grid grid-cols-3 gap-4 pt-2 border-t">
            <div>
              <label className="label">Target table <span className="text-red-500">*</span></label>
              <input className="input font-mono" value={f.target_table} onChange={e => set('target_table', e.target.value)} placeholder="SAP_LQUA" />
            </div>
            <div>
              <label className="label">Write mode</label>
              <select className="input" value={f.write_mode} onChange={e => set('write_mode', e.target.value)}>
                <option value="replace">replace (truncate + load)</option>
                <option value="append">append</option>
              </select>
            </div>
            <div>
              <label className="label">Row limit</label>
              <input type="number" className="input" value={f.row_limit} onChange={e => set('row_limit', e.target.value)} />
            </div>
          </div>

          {/* Trigger */}
          <div className="grid grid-cols-2 gap-4 pt-2 border-t">
            <div>
              <label className="label">Trigger</label>
              <select className="input" value={f.trigger_type} onChange={e => set('trigger_type', e.target.value)}>
                <option value="manual">manual (Run now only)</option>
                <option value="schedule">schedule</option>
              </select>
            </div>
            {f.trigger_type === 'schedule' && (
              <div>
                <label className="label">Frequency</label>
                <select className="input" value={f.sched_freq} onChange={e => set('sched_freq', e.target.value)}>
                  <option value="daily">daily</option>
                  <option value="weekly">weekly</option>
                  <option value="monthly">monthly</option>
                  <option value="every_n_hours">every N hours</option>
                </select>
              </div>
            )}
            {f.trigger_type === 'schedule' && f.sched_freq !== 'every_n_hours' && (
              <div>
                <label className="label">Time (UTC, HH:MM)</label>
                <input className="input" value={f.sched_time} onChange={e => set('sched_time', e.target.value)} placeholder="02:00" />
              </div>
            )}
            {f.trigger_type === 'schedule' && f.sched_freq === 'every_n_hours' && (
              <div>
                <label className="label">Every N hours</label>
                <input type="number" className="input" value={f.sched_hours} onChange={e => set('sched_hours', e.target.value)} />
              </div>
            )}
          </div>

          <label className="flex items-center gap-2 cursor-pointer pt-2">
            <input type="checkbox" className="w-4 h-4 rounded border-gray-300" checked={!!f.enabled} onChange={e => set('enabled', e.target.checked)} />
            <span className="text-sm text-gray-700">Enabled</span>
          </label>
        </div>
        <div className="flex justify-end gap-3 px-6 py-4 border-t sticky bottom-0 bg-white">
          <button onClick={onClose} className="btn-secondary">Cancel</button>
          <button onClick={onSave} disabled={saving} className="btn-primary">{saving ? 'Saving…' : 'Save'}</button>
        </div>
      </div>
    </div>
  )
}
