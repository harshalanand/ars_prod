import { useEffect, useState } from 'react'
import { Database, Save, RefreshCw, Check, AlertCircle, Shield } from 'lucide-react'
import { snowflakeConfigAPI } from '@/services/api'
import useAuthStore from '@/store/authStore'
import toast from 'react-hot-toast'

const MASK = '********'
const empty = {
  account: '', user: '', auth: 'keypair', private_key_path: '', private_key_pwd: '',
  password: '', role: '', warehouse: '', database: 'V2RETAIL', schema: 'BRONZE', enabled: false,
  has_password: false, has_key_pwd: false, last_status: null, last_message: null, last_verified: null,
}

// The single app-wide Snowflake connection. Used by the SAP Snowflake door AND
// the Report Generation scheduler. Rendered inside Settings → Snowflake.
export default function SnowflakeConnectionPage({ embedded = false }) {
  const isSuperAdmin = useAuthStore(s => s.isSuperAdmin?.())
  const isAdmin = useAuthStore(s => s.isSuperAdmin?.() || s.hasRole?.('ADMIN'))
  const [cfg, setCfg] = useState(empty)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)

  useEffect(() => { load() }, [])

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await snowflakeConfigAPI.getConfig()
      setCfg({ ...empty, ...(data?.data || {}) })
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load Snowflake config')
    } finally { setLoading(false) }
  }

  const set = (k, v) => setCfg(p => ({ ...p, [k]: v }))

  const save = async (silent = false) => {
    setSaving(true)
    try {
      const payload = {
        account: cfg.account || '', user: cfg.user || '', auth: cfg.auth || 'keypair',
        private_key_path: cfg.private_key_path || '',
        private_key_pwd: cfg.private_key_pwd || '',   // '********' → keep stored
        password: cfg.password || '',                  // '********' → keep stored
        role: cfg.role || '', warehouse: cfg.warehouse || '',
        database: cfg.database || '', schema: cfg.schema || '',
        enabled: !!cfg.enabled,
      }
      const { data } = await snowflakeConfigAPI.saveConfig(payload)
      if (!silent) toast.success(data?.message || 'Saved')
      await load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Save failed')
    } finally { setSaving(false) }
  }

  const test = async () => {
    setTesting(true)
    try {
      await save(true)   // persist first so the server tests on-screen values
      const { data } = await snowflakeConfigAPI.test()
      if (data?.success) toast.success(data.message || 'Snowflake connected')
      else toast.error(data?.message || 'Snowflake connection failed')
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
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><Database size={24} /> Snowflake</h1>
        </div>
      )}

      <div className="card p-6 space-y-5">
        <div>
          <h3 className="font-semibold text-gray-900 text-lg flex items-center gap-2"><Database size={18} /> Snowflake Connection</h3>
          <p className="text-gray-500 text-sm mt-0.5">
            One connection used everywhere Snowflake is read — the SAP module's <b>Snowflake door</b> and the
            <b> Report Generation</b> scheduler. Key-pair (JWT) or password. Read-only usage.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="label">Account</label>
            <input className="input font-mono" value={cfg.account || ''} disabled={!isSuperAdmin}
              onChange={e => set('account', e.target.value)} placeholder="iafphkw-hh80816" />
          </div>
          <div>
            <label className="label">User</label>
            <input className="input font-mono" value={cfg.user || ''} disabled={!isSuperAdmin}
              onChange={e => set('user', e.target.value)} placeholder="SANTOSH" />
          </div>
          <div>
            <label className="label">Auth {!isSuperAdmin && <span className="text-xs text-gray-400 ml-2">(SUPER_ADMIN only)</span>}</label>
            <select className="input" value={cfg.auth || 'keypair'} disabled={!isSuperAdmin}
              onChange={e => set('auth', e.target.value)}>
              <option value="keypair">Key-pair (JWT)</option>
              <option value="password">Password</option>
            </select>
          </div>
          <div className="flex items-center pt-6">
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" className="w-4 h-4 rounded border-gray-300"
                checked={!!cfg.enabled} disabled={!isSuperAdmin}
                onChange={e => set('enabled', e.target.checked)} />
              <span className="text-sm text-gray-700">Enable Snowflake</span>
            </label>
          </div>

          {(cfg.auth || 'keypair') === 'keypair' ? (
            <>
              <div className="col-span-2">
                <label className="label">Private key file (on the server)</label>
                <input className="input font-mono" value={cfg.private_key_path || ''} disabled={!isSuperAdmin}
                  onChange={e => set('private_key_path', e.target.value)}
                  placeholder="C:\\Users\\santosh.kumar3\\.snowflake\\santosh_rsa.p8" />
              </div>
              <div>
                <label className="label">Key passphrase (if encrypted)</label>
                <input type="password" className="input" value={cfg.private_key_pwd || ''} disabled={!isSuperAdmin}
                  onChange={e => set('private_key_pwd', e.target.value)} placeholder="(none)" />
              </div>
            </>
          ) : (
            <div className="col-span-2">
              <label className="label">Password</label>
              <input type="password" className="input" value={cfg.password || ''} disabled={!isSuperAdmin}
                onChange={e => set('password', e.target.value)} placeholder="••••••••" />
            </div>
          )}

          <div>
            <label className="label">Role</label>
            <input className="input font-mono" value={cfg.role || ''} disabled={!isSuperAdmin}
              onChange={e => set('role', e.target.value)} placeholder="DATA_PLATFORM_ADMIN" />
          </div>
          <div>
            <label className="label">Warehouse</label>
            <input className="input font-mono" value={cfg.warehouse || ''} disabled={!isSuperAdmin}
              onChange={e => set('warehouse', e.target.value)} placeholder="COMPUTE_WH" />
          </div>
          <div>
            <label className="label">Default database</label>
            <input className="input font-mono" value={cfg.database || ''} disabled={!isSuperAdmin}
              onChange={e => set('database', e.target.value)} placeholder="V2RETAIL" />
          </div>
          <div>
            <label className="label">Default schema</label>
            <input className="input font-mono" value={cfg.schema || ''} disabled={!isSuperAdmin}
              onChange={e => set('schema', e.target.value)} placeholder="BRONZE" />
          </div>
        </div>

        <div className="flex items-center gap-3 pt-4 border-t">
          <button onClick={test} disabled={testing || !isAdmin} className="btn-secondary">
            <RefreshCw size={16} className={testing ? 'animate-spin' : ''} /> Save &amp; Test
          </button>
          <button onClick={() => save()} disabled={saving || !isSuperAdmin} className="btn-primary">
            <Save size={16} /> {saving ? 'Saving…' : 'Save Changes'}
          </button>
        </div>

        <p className="text-xs text-gray-400">
          The key file must exist on the ARS server. Secrets are stored encrypted; a secret field shows
          <code> ******** </code> when one is already saved — leave it to keep the stored value.
        </p>
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
