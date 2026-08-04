import { useEffect, useState } from 'react'
import { Server, Save, RefreshCw, Check, AlertCircle, Shield } from 'lucide-react'
import { sapAPI } from '@/services/api'
import useAuthStore from '@/store/authStore'
import useSapUiStore from '@/store/sapUiStore'
import toast from 'react-hot-toast'

const MASK = '********'
const empty = { worker_url: '', api_key: '', default_env: 'prod', display_mode: 'name', enabled: false, has_key: false }

export default function SapConnectionPage({ embedded = false }) {
  const isSuperAdmin = useAuthStore(s => s.isSuperAdmin?.())
  const isAdmin = useAuthStore(s => s.isSuperAdmin?.() || s.hasRole?.('ADMIN'))
  const setDisplayMode = useSapUiStore(s => s.setDisplayMode)
  const [cfg, setCfg] = useState(empty)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [keyTouched, setKeyTouched] = useState(false)

  useEffect(() => { load() }, [])

  const load = async () => {
    setLoading(true); setKeyTouched(false)
    try {
      const { data } = await sapAPI.getConnection()
      const c = data?.data || empty
      setCfg({ ...empty, ...c, api_key: c.has_key ? MASK : '' })
      setDisplayMode(c.display_mode || 'name')   // sync the global preference
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load connection')
    } finally { setLoading(false) }
  }

  const set = (k, v) => setCfg(p => ({ ...p, [k]: v }))

  const save = async () => {
    setSaving(true)
    try {
      const payload = {
        worker_url: cfg.worker_url || '',
        default_env: cfg.default_env || 'prod',
        display_mode: cfg.display_mode || 'name',
        enabled: !!cfg.enabled,
        api_key: keyTouched ? (cfg.api_key || '') : MASK,
      }
      const { data } = await sapAPI.saveConnection(payload)
      setDisplayMode(cfg.display_mode || 'name')   // apply globally right away
      toast.success(data?.message || 'Saved')
      await load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Save failed')
    } finally { setSaving(false) }
  }

  const test = async () => {
    setTesting(true)
    try {
      const { data } = await sapAPI.testConnection()
      if (data?.success) toast.success(data.message || 'Gateway reachable')
      else toast.error(data?.message || 'Connection failed')
      await load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Test failed')
    } finally { setTesting(false) }
  }

  if (loading) return <div className="flex items-center justify-center h-64"><RefreshCw className="animate-spin text-primary-600" size={32} /></div>

  const ok = String(cfg.last_status || '').toLowerCase() === 'connected'

  return (
    <div className="max-w-3xl space-y-6">
      {!embedded && (
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><Server size={24} /> SAP Connection</h1>
          <p className="text-gray-500 text-sm mt-0.5">
            The gateway ARS uses to read data from SAP. ARS calls this MCP worker over HTTPS; the
            worker relays to SAP (RFC / OData) and returns rows. No SAP software runs on this server.
          </p>
        </div>
      )}

      <div className="card p-6 space-y-5">
        <div className="grid grid-cols-2 gap-4">
          <div className="col-span-2">
            <label className="label">Gateway URL</label>
            <input className="input" value={cfg.worker_url || ''} disabled={!isSuperAdmin}
              onChange={e => set('worker_url', e.target.value)}
              placeholder="https://universal-mcp.akash-bab.workers.dev" />
          </div>
          <div className="col-span-2">
            <label className="label">API Key {!isSuperAdmin && <span className="text-xs text-gray-400 ml-2">(SUPER_ADMIN only)</span>}</label>
            <input type="password" className="input disabled:bg-gray-50 disabled:text-gray-400"
              value={cfg.api_key || ''} disabled={!isSuperAdmin}
              onChange={e => { setKeyTouched(true); set('api_key', e.target.value) }}
              onFocus={() => { if (!keyTouched && cfg.api_key === MASK) { setKeyTouched(true); set('api_key', '') } }}
              placeholder="X-API-Key value" />
            <p className="text-xs text-gray-400 mt-1">Stored encrypted (Fernet); never shown again after saving.</p>
          </div>
          <div>
            <label className="label">Default SAP Environment</label>
            <select className="input" value={cfg.default_env || 'prod'} disabled={!isSuperAdmin}
              onChange={e => set('default_env', e.target.value)}>
              <option value="prod">prod</option>
              <option value="qa">qa</option>
              <option value="dev">dev</option>
            </select>
          </div>
          <div className="flex items-center pt-6">
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" className="w-4 h-4 rounded border-gray-300"
                checked={!!cfg.enabled} disabled={!isSuperAdmin}
                onChange={e => set('enabled', e.target.checked)} />
              <span className="text-sm text-gray-700">Enable SAP integration</span>
            </label>
          </div>

          {/* Global field display — applied on every SAP screen. */}
          <div className="col-span-2">
            <label className="label">Field display (applies to every SAP screen)</label>
            <div className="flex gap-2">
              {[
                ['name', 'Field name', 'MATNR'],
                ['label', 'Label', 'Material Number'],
                ['both', 'Both', 'MATNR · Material Number'],
              ].map(([v, l, eg]) => (
                <button key={v} type="button" disabled={!isSuperAdmin}
                  onClick={() => set('display_mode', v)}
                  className={`flex-1 px-3 py-2 rounded border text-left transition-colors disabled:opacity-60 ${
                    (cfg.display_mode || 'name') === v
                      ? 'bg-primary-50 text-primary-700 border-primary-400'
                      : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50'}`}>
                  <div className="text-sm font-semibold">{l}</div>
                  <div className="text-[11px] text-gray-400 font-mono truncate">{eg}</div>
                </button>
              ))}
            </div>
            <p className="text-xs text-gray-400 mt-1">
              Controls whether SAP columns show as the technical field, its SAP label, or both — everywhere in the SAP module.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3 pt-4 border-t">
          <button onClick={test} disabled={testing || !isAdmin} className="btn-secondary">
            {testing ? <RefreshCw size={16} className="animate-spin" /> : <RefreshCw size={16} />} Test Connection
          </button>
          <button onClick={() => save()} disabled={saving || !isSuperAdmin} className="btn-primary">
            <Save size={16} /> {saving ? 'Saving…' : 'Save Changes'}
          </button>
        </div>
      </div>

      {/* Status */}
      <div className="card p-6">
        <h3 className="font-semibold text-gray-900 text-base flex items-center gap-2 mb-3"><Shield size={18} /> Status</h3>
        <div className={`p-4 rounded-lg flex items-center gap-2 ${ok ? 'bg-green-50 border border-green-200' : 'bg-gray-50 border border-gray-200'}`}>
          {ok ? <Check size={18} className="text-green-600" /> : <AlertCircle size={18} className="text-gray-400" />}
          <span className={ok ? 'text-green-700 font-medium' : 'text-gray-600'}>
            {cfg.last_status || 'Not Verified'}{cfg.last_message ? ` — ${cfg.last_message}` : ''}
          </span>
        </div>
        {cfg.last_verified && <p className="text-xs text-gray-400 mt-2">Last verified {new Date(cfg.last_verified).toLocaleString()}</p>}
      </div>
    </div>
  )
}
