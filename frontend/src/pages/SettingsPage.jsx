import { useState, useEffect } from 'react'
import { Settings, Database, Mail, Palette, Shield, Server, Check, AlertCircle, AlertTriangle, RefreshCw, Send, Save, HardDrive, Trash2, Download, Play, Users, Cpu, Clock, Table2, Eye, Upload, FileDown, Edit3 } from 'lucide-react'
import { settingsAPI, tablesAPI, maintenanceAPI, listingAPI } from '@/services/api'
import useAuthStore from '@/store/authStore'
import toast from 'react-hot-toast'

const tabs = [
  { id: 'database', label: 'Database', icon: Database },
  { id: 'email', label: 'Email', icon: Mail },
  { id: 'application', label: 'Application', icon: Settings },
  { id: 'tables', label: 'Table Permissions', icon: Table2 },
  { id: 'ui', label: 'UI Preferences', icon: Palette },
  { id: 'backup', label: 'Backup', icon: HardDrive },
  { id: 'system', label: 'System Info', icon: Server },
  { id: 'danger', label: 'Danger Zone', icon: AlertTriangle },
]

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState('database')
  const [settings, setSettings] = useState({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testingDb, setTestingDb] = useState(false)
  const [testingEmail, setTestingEmail] = useState(false)
  const [testEmail, setTestEmail] = useState('')
  const [systemInfo, setSystemInfo] = useState(null)
  const [dbStatus, setDbStatus] = useState(null)
  const [backups, setBackups] = useState([])
  const [creatingBackup, setCreatingBackup] = useState(false)
  const [backupsLoading, setBackupsLoading] = useState(false)

  // Parking mode (admin-only) — Single / Multiple parked sessions.
  // Persisted server-side under AppSettings `listing.allow_multi_parked`.
  // The toggle on the Listing page was removed in favour of this setting,
  // so only ADMIN / SUPER_ADMIN can flip it.
  const isAdmin = useAuthStore(s => s.isSuperAdmin?.() || s.hasRole?.('ADMIN'))
  const [parkingMultiple, setParkingMultiple] = useState(false)
  const [parkingLoading, setParkingLoading] = useState(false)
  const [parkingSaving, setParkingSaving] = useState(false)

  // Danger Zone — transactional reset
  const [resetIncludeMsa, setResetIncludeMsa] = useState(false)
  const [resetPreview, setResetPreview] = useState(null)
  const [resetPreviewLoading, setResetPreviewLoading] = useState(false)
  const [resetRunning, setResetRunning] = useState(false)
  const [resetConfirmText, setResetConfirmText] = useState('')
  const [resetReport, setResetReport] = useState(null)

  useEffect(() => {
    loadSettings()
    loadSystemInfo()
  }, [])

  useEffect(() => {
    if (activeTab === 'backup') {
      loadBackups()
    }
    if (activeTab === 'danger') {
      loadResetPreview(resetIncludeMsa)
    }
    if (activeTab === 'application') {
      loadParkingMode()
    }
  }, [activeTab])

  const loadParkingMode = async () => {
    setParkingLoading(true)
    try {
      const { data } = await listingAPI.getParkingMode()
      setParkingMultiple(!!data?.data?.allow_multi_parked)
    } catch (e) {
      // Read is open to any user — surface unexpected errors only.
      console.warn('parking-mode load failed', e)
    } finally {
      setParkingLoading(false)
    }
  }

  const handleParkingChange = async (next) => {
    if (!isAdmin) {
      toast.error('Only admins can change the parking mode.')
      return
    }
    setParkingSaving(true)
    const prev = parkingMultiple
    setParkingMultiple(next)  // optimistic
    try {
      await listingAPI.setParkingMode(next)
      toast.success(`Parking mode → ${next ? 'Multiple' : 'Single'}`)
    } catch (e) {
      setParkingMultiple(prev)  // revert on failure
      toast.error(e.response?.data?.detail || 'Failed to update parking mode')
    } finally {
      setParkingSaving(false)
    }
  }

  const loadResetPreview = async (includeMsa = resetIncludeMsa) => {
    setResetPreviewLoading(true)
    try {
      const { data } = await maintenanceAPI.resetPreview(includeMsa)
      setResetPreview(data.report || null)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Preview failed')
      setResetPreview(null)
    } finally {
      setResetPreviewLoading(false)
    }
  }

  const runTransactionalReset = async () => {
    if (resetConfirmText !== 'RESET') {
      toast.error('Type RESET to confirm.')
      return
    }
    setResetRunning(true)
    setResetReport(null)
    try {
      const { data } = await maintenanceAPI.resetTransactionalData(resetIncludeMsa)
      setResetReport(data.report || null)
      const t = data.report?.totals
      toast.success(
        `Reset complete — cleared ${t?.cleared || 0} table(s), ${t?.rows_deleted?.toLocaleString?.() || 0} rows.`
      )
      setResetConfirmText('')
      loadResetPreview(resetIncludeMsa)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Reset failed')
    } finally {
      setResetRunning(false)
    }
  }

  const loadSettings = async () => {
    setLoading(true)
    try {
      const { data } = await settingsAPI.getAll()
      setSettings(data.data || {})
    } catch (e) {
      // Settings might not exist yet, use defaults
      setSettings({
        database: {
          server: '', port: '', system_database: '', data_database: '',
          username: 'sa', password: '',
          driver: 'ODBC Driver 18 for SQL Server',
          trust_cert: 'yes', encrypt: 'no',
        },
        email: { smtp_server: '', smtp_port: 587, smtp_username: '', smtp_password: '', from_address: '', use_tls: true, notifications_enabled: false },
        application: { app_name: 'ARS', max_upload_size_mb: 500, session_timeout_minutes: 60, enable_audit_logging: true, enable_row_level_security: true, default_page_size: 50, max_export_rows: 500000 },
        ui: { primary_color: '#4f46e5', sidebar_collapsed: false, show_row_numbers: true, date_format: 'YYYY-MM-DD', number_format: 'en-US' },
      })
    } finally {
      setLoading(false)
    }
  }

  const loadSystemInfo = async () => {
    try {
      const { data } = await settingsAPI.systemInfo()
      setSystemInfo(data.data)
    } catch {}
  }

  const loadBackups = async () => {
    setBackupsLoading(true)
    try {
      const { data } = await settingsAPI.listBackups()
      setBackups(data.data?.backups || [])
    } catch (e) {
      toast.error('Failed to load backups')
    } finally {
      setBackupsLoading(false)
    }
  }

  const createBackup = async (database) => {
    setCreatingBackup(true)
    try {
      const { data } = await settingsAPI.createBackup(database)
      toast.success(data.message || 'Backup created')
      loadBackups()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Backup failed')
    } finally {
      setCreatingBackup(false)
    }
  }

  const deleteBackup = async (filename) => {
    if (!confirm(`Delete backup "${filename}"?`)) return
    try {
      await settingsAPI.deleteBackup(filename)
      toast.success('Backup deleted')
      loadBackups()
    } catch (e) {
      toast.error('Failed to delete backup')
    }
  }

  const handleSave = async (category) => {
    setSaving(true)
    try {
      // Database tab uses the apply flow: test → save JSON + .env →
      // hot-reload engines. No process restart.
      if (category === 'database') {
        const pending = toast.loading('Verifying connection and applying…')
        try {
          const { data } = await settingsAPI.applyDatabase(settings.database || {})
          toast.dismiss(pending)
          toast.success(
            data?.message || 'Database settings applied. App is now using the new server.',
            { duration: 5000 },
          )
          loadSettings()
          loadSystemInfo()
        } catch (e) {
          toast.dismiss(pending)
          const detail = e.response?.data?.detail
          if (typeof detail === 'object' && detail?.message) {
            toast.error(`${detail.message} ${detail.hint || ''}`.trim(), { duration: 10000 })
          } else {
            toast.error(detail || 'Failed to apply database settings')
          }
        }
        return
      }

      // All other categories: simple PUT.
      const { data } = await settingsAPI.update(category, settings[category])
      toast.success(data?.message || `${category} settings saved`)
      loadSettings()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to save settings')
    } finally {
      setSaving(false)
    }
  }

  const updateSetting = (category, key, value) => {
    setSettings(prev => ({
      ...prev,
      [category]: { ...prev[category], [key]: value }
    }))
  }

  const testDbConnection = async () => {
    setTestingDb(true)
    setDbStatus(null)
    try {
      // Send the values currently in the form so the user can verify
      // BEFORE saving / restarting.
      const { data } = await settingsAPI.testConnection(settings.database || {})
      setDbStatus({ success: true, ...data.data })
      const sysOk  = data.data?.system_db?.status === 'connected'
      const dataOk = data.data?.data_db?.status === 'connected'
      if (sysOk && dataOk)        toast.success(data.message || 'Both databases connected')
      else if (sysOk && !dataOk)  toast(data.message || 'System DB connected; Data DB missing.', { icon: 'i', duration: 6000 })
      else                         toast.error(data.message || 'Database connection failed')
    } catch (e) {
      setDbStatus({ success: false, error: e.response?.data?.detail || 'Connection failed' })
      toast.error(e.response?.data?.detail || 'Database connection failed')
    } finally {
      setTestingDb(false)
    }
  }

  const sendTestEmail = async () => {
    if (!testEmail) return toast.error('Enter email address')
    setTestingEmail(true)
    try {
      await settingsAPI.testEmail(testEmail)
      toast.success(`Test email sent to ${testEmail}`)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to send test email')
    } finally {
      setTestingEmail(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="animate-spin text-primary-600" size={32} />
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Settings</h1>
        <p className="text-gray-500 text-sm mt-0.5">Configure application settings, database, email, and preferences</p>
      </div>

      <div className="flex gap-6">
        {/* Sidebar tabs */}
        <div className="w-48 shrink-0">
          <div className="card p-2 space-y-1">
            {tabs.map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                  activeTab === tab.id
                    ? 'bg-primary-50 text-primary-700'
                    : 'text-gray-600 hover:bg-gray-50'
                }`}
              >
                <tab.icon size={18} />
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* Content */}
        <div className="flex-1">
          {/* Database Settings */}
          {activeTab === 'database' && (
            <div className="card p-6 space-y-6">
              <h3 className="font-semibold text-gray-900 text-lg flex items-center gap-2">
                <Database size={20} /> Database Configuration
              </h3>
              
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">SQL Server</label>
                  <input
                    value={settings.database?.server || ''}
                    onChange={e => updateSetting('database', 'server', e.target.value)}
                    className="input"
                    placeholder="192.168.150.249 or server\\instance"
                  />
                </div>
                <div>
                  <label className="label">Port (optional)</label>
                  <input
                    value={settings.database?.port || ''}
                    onChange={e => updateSetting('database', 'port', e.target.value)}
                    className="input"
                    placeholder="1433"
                  />
                </div>
                <div>
                  <label className="label">System Database</label>
                  <input
                    value={settings.database?.system_database || ''}
                    onChange={e => updateSetting('database', 'system_database', e.target.value)}
                    className="input"
                    placeholder="Claude"
                  />
                </div>
                <div>
                  <label className="label">Data Database</label>
                  <input
                    value={settings.database?.data_database || ''}
                    onChange={e => updateSetting('database', 'data_database', e.target.value)}
                    className="input"
                    placeholder="Rep_data"
                  />
                </div>
                <div>
                  <label className="label">Username</label>
                  <input
                    value={settings.database?.username || ''}
                    onChange={e => updateSetting('database', 'username', e.target.value)}
                    className="input"
                    placeholder="sa"
                    autoComplete="off"
                  />
                </div>
                <div>
                  <label className="label">Password</label>
                  <input
                    type="password"
                    value={settings.database?.password || ''}
                    onChange={e => updateSetting('database', 'password', e.target.value)}
                    className="input"
                    placeholder="••••••••"
                    autoComplete="new-password"
                  />
                </div>
                <div>
                  <label className="label">ODBC Driver</label>
                  <input
                    value={settings.database?.driver || ''}
                    onChange={e => updateSetting('database', 'driver', e.target.value)}
                    className="input"
                    placeholder="ODBC Driver 18 for SQL Server"
                  />
                </div>
                <div>
                  <label className="label">Trust Server Certificate</label>
                  <select
                    value={settings.database?.trust_cert || 'yes'}
                    onChange={e => updateSetting('database', 'trust_cert', e.target.value)}
                    className="input"
                  >
                    <option value="yes">yes (local SQL Server)</option>
                    <option value="no">no (Azure SQL)</option>
                  </select>
                </div>
                <div>
                  <label className="label">Encrypt Connection</label>
                  <select
                    value={settings.database?.encrypt || 'no'}
                    onChange={e => updateSetting('database', 'encrypt', e.target.value)}
                    className="input"
                  >
                    <option value="no">no (LAN)</option>
                    <option value="yes">yes (Azure / WAN)</option>
                  </select>
                </div>
              </div>

              <div className="text-xs text-gray-500 bg-amber-50 border border-amber-200 rounded p-3">
                <strong>How it works:</strong> Click <em>Save Changes</em> to verify the connection,
                write the new credentials to <code>backend/app_settings.json</code> and <code>backend/.env</code>,
                and hot-reload the database engines so the running application connects to the new server
                immediately — no process restart, no downtime. If the connection test fails, nothing is saved.
                Use <em>Test Connection</em> first if you want to validate values without saving.
              </div>

              <div className="flex items-center gap-4 pt-4 border-t">
                <button onClick={testDbConnection} disabled={testingDb} className="btn-secondary">
                  {testingDb ? <RefreshCw size={16} className="animate-spin" /> : <Server size={16} />}
                  Test Connection
                </button>
                <button onClick={() => handleSave('database')} disabled={saving} className="btn-primary">
                  <Save size={16} /> Save Changes
                </button>
              </div>

              {dbStatus && (
                <div className="space-y-3">
                  {/* System DB Status */}
                  <div className={`p-4 rounded-lg ${dbStatus.system_db?.status === 'connected' ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}`}>
                    <div className="flex items-center gap-2">
                      {dbStatus.system_db?.status === 'connected' ? (
                        <Check size={18} className="text-green-600" />
                      ) : (
                        <AlertCircle size={18} className="text-red-600" />
                      )}
                      <span className={dbStatus.system_db?.status === 'connected' ? 'text-green-700 font-medium' : 'text-red-700'}>
                        System DB: {dbStatus.system_db?.status === 'connected'
                          ? `Connected to ${dbStatus.system_db.database}`
                          : dbStatus.system_db?.error || 'Disconnected'}
                      </span>
                    </div>
                    {dbStatus.system_db?.hint && (
                      <div className="text-xs text-red-600 mt-1 ml-6 font-medium">{dbStatus.system_db.hint}</div>
                    )}
                    {dbStatus.system_db?.server_version && (
                      <div className="text-xs text-gray-500 mt-1 ml-6">{dbStatus.system_db.server_version}</div>
                    )}
                  </div>

                  {/* Data DB Status */}
                  <div className={`p-4 rounded-lg ${dbStatus.data_db?.status === 'connected' ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}`}>
                    <div className="flex items-center gap-2">
                      {dbStatus.data_db?.status === 'connected' ? (
                        <Check size={18} className="text-green-600" />
                      ) : (
                        <AlertCircle size={18} className="text-red-600" />
                      )}
                      <span className={dbStatus.data_db?.status === 'connected' ? 'text-green-700 font-medium' : 'text-red-700'}>
                        Data DB: {dbStatus.data_db?.status === 'connected'
                          ? `Connected to ${dbStatus.data_db.database}`
                          : dbStatus.data_db?.error || 'Disconnected'}
                      </span>
                    </div>
                    {dbStatus.data_db?.hint && (
                      <div className="text-xs text-red-600 mt-1 ml-6 font-medium">{dbStatus.data_db.hint}</div>
                    )}
                    {dbStatus.data_db?.server_version && (
                      <div className="text-xs text-gray-500 mt-1 ml-6">{dbStatus.data_db.server_version}</div>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Email Settings */}
          {activeTab === 'email' && (
            <div className="card p-6 space-y-6">
              <h3 className="font-semibold text-gray-900 text-lg flex items-center gap-2">
                <Mail size={20} /> Email Configuration
              </h3>
              
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">SMTP Server</label>
                  <input
                    value={settings.email?.smtp_server || ''}
                    onChange={e => updateSetting('email', 'smtp_server', e.target.value)}
                    className="input"
                    placeholder="smtp.gmail.com"
                  />
                </div>
                <div>
                  <label className="label">SMTP Port</label>
                  <input
                    type="number"
                    value={settings.email?.smtp_port || 587}
                    onChange={e => updateSetting('email', 'smtp_port', parseInt(e.target.value))}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Username</label>
                  <input
                    value={settings.email?.smtp_username || ''}
                    onChange={e => updateSetting('email', 'smtp_username', e.target.value)}
                    className="input"
                    placeholder="user@example.com"
                  />
                </div>
                <div>
                  <label className="label">Password</label>
                  <input
                    type="password"
                    value={settings.email?.smtp_password || ''}
                    onChange={e => updateSetting('email', 'smtp_password', e.target.value)}
                    className="input"
                    placeholder="••••••••"
                  />
                </div>
                <div>
                  <label className="label">From Address</label>
                  <input
                    value={settings.email?.from_address || ''}
                    onChange={e => updateSetting('email', 'from_address', e.target.value)}
                    className="input"
                    placeholder="noreply@company.com"
                  />
                </div>
                <div className="flex items-center gap-4 pt-6">
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={settings.email?.use_tls || false}
                      onChange={e => updateSetting('email', 'use_tls', e.target.checked)}
                      className="w-4 h-4 rounded border-gray-300"
                    />
                    <span className="text-sm text-gray-700">Use TLS</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={settings.email?.notifications_enabled || false}
                      onChange={e => updateSetting('email', 'notifications_enabled', e.target.checked)}
                      className="w-4 h-4 rounded border-gray-300"
                    />
                    <span className="text-sm text-gray-700">Enable Notifications</span>
                  </label>
                </div>
              </div>

              <div className="flex items-center gap-4 pt-4 border-t">
                <div className="flex-1 flex items-center gap-2">
                  <input
                    value={testEmail}
                    onChange={e => setTestEmail(e.target.value)}
                    className="input"
                    placeholder="test@example.com"
                  />
                  <button onClick={sendTestEmail} disabled={testingEmail} className="btn-secondary shrink-0">
                    {testingEmail ? <RefreshCw size={16} className="animate-spin" /> : <Send size={16} />}
                    Send Test
                  </button>
                </div>
                <button onClick={() => handleSave('email')} disabled={saving} className="btn-primary">
                  <Save size={16} /> Save Changes
                </button>
              </div>
            </div>
          )}

          {/* Application Settings */}
          {activeTab === 'application' && (
            <div className="card p-6 space-y-6">
              <h3 className="font-semibold text-gray-900 text-lg flex items-center gap-2">
                <Settings size={20} /> Application Settings
              </h3>
              
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Application Name</label>
                  <input
                    value={settings.application?.app_name || ''}
                    onChange={e => updateSetting('application', 'app_name', e.target.value)}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Max Upload Size (MB)</label>
                  <input
                    type="number"
                    value={settings.application?.max_upload_size_mb || 500}
                    onChange={e => updateSetting('application', 'max_upload_size_mb', parseInt(e.target.value))}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Session Timeout (minutes)</label>
                  <input
                    type="number"
                    value={settings.application?.session_timeout_minutes || 60}
                    onChange={e => updateSetting('application', 'session_timeout_minutes', parseInt(e.target.value))}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Default Page Size</label>
                  <input
                    type="number"
                    value={settings.application?.default_page_size || 50}
                    onChange={e => updateSetting('application', 'default_page_size', parseInt(e.target.value))}
                    className="input"
                  />
                </div>
                <div>
                  <label className="label">Max Export Rows</label>
                  <input
                    type="number"
                    value={settings.application?.max_export_rows || 500000}
                    onChange={e => updateSetting('application', 'max_export_rows', parseInt(e.target.value))}
                    className="input"
                  />
                </div>
              </div>

              <div className="flex flex-wrap gap-6 pt-4 border-t">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={settings.application?.enable_audit_logging || false}
                    onChange={e => updateSetting('application', 'enable_audit_logging', e.target.checked)}
                    className="w-4 h-4 rounded border-gray-300"
                  />
                  <span className="text-sm text-gray-700">Enable Audit Logging</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={settings.application?.enable_row_level_security || false}
                    onChange={e => updateSetting('application', 'enable_row_level_security', e.target.checked)}
                    className="w-4 h-4 rounded border-gray-300"
                  />
                  <span className="text-sm text-gray-700">Enable Row-Level Security</span>
                </label>
              </div>

              {/* Parking mode — admin-only. Moved here from the Listing page. */}
              <div className="pt-4 border-t">
                <div className="flex items-center justify-between gap-4 flex-wrap">
                  <div>
                    <div className="text-sm font-semibold text-gray-900 flex items-center gap-2">
                      <Shield size={14} className="text-amber-600"/> Parking Mode
                      {!isAdmin && (
                        <span className="text-xs font-normal text-gray-500 italic">
                          (admin only — read-only for you)
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-gray-500 mt-0.5 max-w-xl">
                      Controls whether new listing runs are blocked while a parked session is awaiting review.
                      &nbsp;<b>Single</b> (default) — block until the pending parked session is approved/rejected.
                      &nbsp;<b>Multiple</b> — allow new runs to stack alongside pending parked sessions.
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {[['single', 'Single', false], ['multi', 'Multiple', true]].map(([v, l, val]) => {
                      const on = val === !!parkingMultiple
                      return (
                        <button
                          key={v}
                          type="button"
                          disabled={!isAdmin || parkingLoading || parkingSaving}
                          onClick={() => handleParkingChange(val)}
                          className={
                            'px-3 py-1.5 text-xs font-semibold rounded border transition ' +
                            (on
                              ? 'bg-amber-100 text-amber-800 border-amber-500'
                              : 'bg-white text-gray-600 border-gray-300 hover:bg-gray-50') +
                            (!isAdmin ? ' opacity-60 cursor-not-allowed' : '')
                          }
                        >
                          {l}
                        </button>
                      )
                    })}
                    {(parkingLoading || parkingSaving) && (
                      <span className="text-xs text-gray-400">…</span>
                    )}
                  </div>
                </div>
              </div>

              <div className="flex justify-end pt-4 border-t">
                <button onClick={() => handleSave('application')} disabled={saving} className="btn-primary">
                  <Save size={16} /> Save Changes
                </button>
              </div>
            </div>
          )}

          {/* Table Permissions */}
          {activeTab === 'tables' && (
            <TablePermissionsTab />
          )}

          {/* UI Settings */}
          {activeTab === 'ui' && (
            <div className="card p-6 space-y-6">
              <h3 className="font-semibold text-gray-900 text-lg flex items-center gap-2">
                <Palette size={20} /> UI Preferences
              </h3>
              
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Primary Color</label>
                  <div className="flex items-center gap-2">
                    <input
                      type="color"
                      value={settings.ui?.primary_color || '#4f46e5'}
                      onChange={e => updateSetting('ui', 'primary_color', e.target.value)}
                      className="w-12 h-10 rounded border border-gray-300 cursor-pointer"
                    />
                    <input
                      value={settings.ui?.primary_color || '#4f46e5'}
                      onChange={e => updateSetting('ui', 'primary_color', e.target.value)}
                      className="input flex-1"
                    />
                  </div>
                </div>
                <div>
                  <label className="label">Date Format</label>
                  <select
                    value={settings.ui?.date_format || 'YYYY-MM-DD'}
                    onChange={e => updateSetting('ui', 'date_format', e.target.value)}
                    className="input"
                  >
                    <option value="YYYY-MM-DD">YYYY-MM-DD</option>
                    <option value="DD/MM/YYYY">DD/MM/YYYY</option>
                    <option value="MM/DD/YYYY">MM/DD/YYYY</option>
                    <option value="DD-MMM-YYYY">DD-MMM-YYYY</option>
                  </select>
                </div>
                <div>
                  <label className="label">Number Format</label>
                  <select
                    value={settings.ui?.number_format || 'en-US'}
                    onChange={e => updateSetting('ui', 'number_format', e.target.value)}
                    className="input"
                  >
                    <option value="en-US">English (US) - 1,234.56</option>
                    <option value="en-GB">English (UK) - 1,234.56</option>
                    <option value="de-DE">German - 1.234,56</option>
                    <option value="fr-FR">French - 1 234,56</option>
                    <option value="en-IN">Indian - 1,23,456.78</option>
                  </select>
                </div>
              </div>

              <div className="flex flex-wrap gap-6 pt-4 border-t">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={settings.ui?.show_row_numbers || false}
                    onChange={e => updateSetting('ui', 'show_row_numbers', e.target.checked)}
                    className="w-4 h-4 rounded border-gray-300"
                  />
                  <span className="text-sm text-gray-700">Show Row Numbers in Tables</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={settings.ui?.sidebar_collapsed || false}
                    onChange={e => updateSetting('ui', 'sidebar_collapsed', e.target.checked)}
                    className="w-4 h-4 rounded border-gray-300"
                  />
                  <span className="text-sm text-gray-700">Collapse Sidebar by Default</span>
                </label>
              </div>

              <div className="flex justify-end pt-4 border-t">
                <button onClick={() => handleSave('ui')} disabled={saving} className="btn-primary">
                  <Save size={16} /> Save Changes
                </button>
              </div>
            </div>
          )}

          {/* Backup */}
          {activeTab === 'backup' && (
            <div className="card p-6 space-y-6">
              <h3 className="font-semibold text-gray-900 text-lg flex items-center gap-2">
                <HardDrive size={20} /> Database Backup
              </h3>
              
              {/* Create Backup */}
              <div className="space-y-4">
                <h4 className="font-medium text-gray-700">Create New Backup</h4>
                <div className="flex flex-wrap gap-3">
                  <button 
                    onClick={() => createBackup('system')} 
                    disabled={creatingBackup}
                    className="btn-secondary"
                  >
                    {creatingBackup ? <RefreshCw size={16} className="animate-spin" /> : <Play size={16} />}
                    Backup System DB
                  </button>
                  <button 
                    onClick={() => createBackup('data')} 
                    disabled={creatingBackup}
                    className="btn-secondary"
                  >
                    {creatingBackup ? <RefreshCw size={16} className="animate-spin" /> : <Play size={16} />}
                    Backup Data DB
                  </button>
                  <button 
                    onClick={() => createBackup('both')} 
                    disabled={creatingBackup}
                    className="btn-primary"
                  >
                    {creatingBackup ? <RefreshCw size={16} className="animate-spin" /> : <HardDrive size={16} />}
                    Backup Both Databases
                  </button>
                </div>
              </div>

              {/* Backup List */}
              <div className="space-y-4 pt-4 border-t">
                <div className="flex items-center justify-between">
                  <h4 className="font-medium text-gray-700">Available Backups</h4>
                  <button onClick={loadBackups} disabled={backupsLoading} className="text-sm text-primary-600 hover:underline flex items-center gap-1">
                    <RefreshCw size={14} className={backupsLoading ? 'animate-spin' : ''} />
                    Refresh
                  </button>
                </div>
                
                {backupsLoading ? (
                  <div className="text-center py-8 text-gray-400">
                    <RefreshCw className="animate-spin mx-auto mb-2" size={24} />
                    Loading backups...
                  </div>
                ) : backups.length === 0 ? (
                  <div className="text-center py-8 text-gray-500">
                    <HardDrive size={32} className="mx-auto mb-2 text-gray-300" />
                    <p>No backups found</p>
                    <p className="text-sm text-gray-400 mt-1">Create your first backup above</p>
                  </div>
                ) : (
                  <div className="divide-y divide-gray-100 rounded-lg border border-gray-200 overflow-hidden">
                    {backups.map(backup => (
                      <div key={backup.filename} className="flex items-center justify-between px-4 py-3 hover:bg-gray-50">
                        <div className="flex items-center gap-3">
                          <div className={`w-2 h-2 rounded-full ${backup.database === 'system' ? 'bg-blue-500' : 'bg-green-500'}`} />
                          <div>
                            <div className="font-medium text-gray-900 text-sm">{backup.filename}</div>
                            <div className="text-xs text-gray-500">
                              {new Date(backup.created).toLocaleString()} • {backup.size_mb} MB • {backup.database === 'system' ? 'System DB' : 'Data DB'}
                            </div>
                          </div>
                        </div>
                        <button 
                          onClick={() => deleteBackup(backup.filename)}
                          className="p-2 text-red-500 hover:bg-red-50 rounded-lg"
                          title="Delete backup"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* System Info */}
          {activeTab === 'system' && (
            <div className="card p-6 space-y-6">
              <h3 className="font-semibold text-gray-900 text-lg flex items-center gap-2">
                <Server size={20} /> System Information
              </h3>
              
              {systemInfo ? (
                <div className="space-y-6">
                  {/* Server Info */}
                  <div>
                    <h4 className="font-medium text-gray-700 mb-3 flex items-center gap-2">
                      <Server size={16} /> Server
                    </h4>
                    <div className="grid grid-cols-2 gap-4">
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="text-sm text-gray-500">Platform</div>
                        <div className="font-medium text-gray-900 mt-1">{systemInfo.platform}</div>
                      </div>
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="text-sm text-gray-500">Hostname</div>
                        <div className="font-medium text-gray-900 mt-1">{systemInfo.hostname}</div>
                      </div>
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="text-sm text-gray-500">Processor</div>
                        <div className="font-medium text-gray-900 mt-1 text-sm">{systemInfo.processor}</div>
                      </div>
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="text-sm text-gray-500">Python Version</div>
                        <div className="font-mono text-sm text-gray-700 mt-1">{systemInfo.python_version}</div>
                      </div>
                    </div>
                  </div>

                  {/* Resource Usage */}
                  <div>
                    <h4 className="font-medium text-gray-700 mb-3 flex items-center gap-2">
                      <Cpu size={16} /> Resource Usage
                    </h4>
                    <div className="grid grid-cols-3 gap-4">
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-sm text-gray-500">CPU</span>
                          <span className="text-sm font-semibold text-gray-900">{systemInfo.cpu_percent}%</span>
                        </div>
                        <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
                          <div 
                            className={`h-full rounded-full ${systemInfo.cpu_percent > 80 ? 'bg-red-500' : systemInfo.cpu_percent > 50 ? 'bg-yellow-500' : 'bg-green-500'}`}
                            style={{ width: `${systemInfo.cpu_percent}%` }}
                          />
                        </div>
                      </div>
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-sm text-gray-500">Memory</span>
                          <span className="text-sm font-semibold text-gray-900">{systemInfo.memory_percent}%</span>
                        </div>
                        <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
                          <div 
                            className={`h-full rounded-full ${systemInfo.memory_percent > 80 ? 'bg-red-500' : systemInfo.memory_percent > 50 ? 'bg-yellow-500' : 'bg-green-500'}`}
                            style={{ width: `${systemInfo.memory_percent}%` }}
                          />
                        </div>
                        <div className="text-xs text-gray-400 mt-1">{systemInfo.memory_used_gb} / {systemInfo.memory_total_gb} GB</div>
                      </div>
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="flex items-center justify-between mb-2">
                          <span className="text-sm text-gray-500">Disk</span>
                          <span className="text-sm font-semibold text-gray-900">{systemInfo.disk_percent}%</span>
                        </div>
                        <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
                          <div 
                            className={`h-full rounded-full ${systemInfo.disk_percent > 80 ? 'bg-red-500' : systemInfo.disk_percent > 50 ? 'bg-yellow-500' : 'bg-green-500'}`}
                            style={{ width: `${systemInfo.disk_percent}%` }}
                          />
                        </div>
                        <div className="text-xs text-gray-400 mt-1">{systemInfo.disk_used_gb} / {systemInfo.disk_total_gb} GB</div>
                      </div>
                    </div>
                  </div>

                  {/* Database Stats */}
                  <div>
                    <h4 className="font-medium text-gray-700 mb-3 flex items-center gap-2">
                      <Database size={16} /> Database Statistics
                    </h4>
                    <div className="grid grid-cols-2 gap-4">
                      <div className="p-4 bg-blue-50 rounded-lg border border-blue-100">
                        <div className="text-sm text-blue-600 font-medium">System Database</div>
                        <div className="text-2xl font-bold text-blue-800 mt-1">{systemInfo.system_db?.size_mb || 0} MB</div>
                      </div>
                      <div className="p-4 bg-green-50 rounded-lg border border-green-100">
                        <div className="text-sm text-green-600 font-medium">Data Database</div>
                        <div className="text-2xl font-bold text-green-800 mt-1">{systemInfo.data_db?.size_mb || 0} MB</div>
                        <div className="text-xs text-green-600 mt-1">{systemInfo.data_db?.tables || 0} tables • {systemInfo.data_db?.columns || 0} columns</div>
                      </div>
                    </div>
                  </div>

                  {/* User Stats */}
                  <div>
                    <h4 className="font-medium text-gray-700 mb-3 flex items-center gap-2">
                      <Users size={16} /> Users
                    </h4>
                    <div className="grid grid-cols-3 gap-4">
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="text-sm text-gray-500">Current User</div>
                        <div className="font-medium text-gray-900 mt-1">{systemInfo.current_user}</div>
                      </div>
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="text-sm text-gray-500">Active Users (24h)</div>
                        <div className="font-medium text-gray-900 mt-1">{systemInfo.active_users_24h}</div>
                      </div>
                      <div className="p-4 bg-gray-50 rounded-lg">
                        <div className="text-sm text-gray-500">Total Users</div>
                        <div className="font-medium text-gray-900 mt-1">{systemInfo.total_users}</div>
                      </div>
                    </div>
                  </div>

                  {/* Uptime */}
                  <div>
                    <h4 className="font-medium text-gray-700 mb-3 flex items-center gap-2">
                      <Clock size={16} /> System Uptime
                    </h4>
                    <div className="p-4 bg-gray-50 rounded-lg inline-block">
                      <div className="text-2xl font-bold text-gray-900">{systemInfo.uptime_formatted}</div>
                      <div className="text-sm text-gray-500 mt-1">System has been running</div>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="text-center py-8 text-gray-400">
                  <RefreshCw className="animate-spin mx-auto mb-2" size={24} />
                  Loading system info...
                </div>
              )}

              <div className="flex justify-end pt-4 border-t">
                <button onClick={loadSystemInfo} className="btn-secondary">
                  <RefreshCw size={16} /> Refresh
                </button>
              </div>
            </div>
          )}

          {/* Danger Zone — Transactional Data Reset */}
          {activeTab === 'danger' && (
            <div className="card p-6 space-y-6 border-2 border-red-200">
              <div>
                <h3 className="font-semibold text-red-700 text-lg flex items-center gap-2">
                  <AlertTriangle size={20} /> Danger Zone — Reset Transactional Data
                </h3>
                <p className="text-sm text-gray-600 mt-2">
                  Wipes all transactional rows in BOTH databases — uploads, allocation runs,
                  MSA results, parked / history snapshots, sessions, audit logs.{' '}
                  <span className="font-medium text-gray-800">
                    Master tables (RBAC, RLS, retail masters, settings, presets) are preserved.
                  </span>{' '}
                  Tables are auto-discovered every run by naming convention, so newly added
                  transactional tables are picked up automatically.
                </p>
              </div>

              <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 text-sm text-amber-900">
                <strong>How clear works:</strong> TRUNCATE is used where possible (no incoming
                foreign keys), DELETE is the fallback. Identity columns are reseeded to 0.
              </div>

              {/* Options */}
              <div className="space-y-3">
                <label className="flex items-start gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={resetIncludeMsa}
                    onChange={e => {
                      setResetIncludeMsa(e.target.checked)
                      loadResetPreview(e.target.checked)
                    }}
                    className="w-4 h-4 mt-0.5 rounded text-red-600"
                  />
                  <span className="text-sm">
                    <span className="font-medium text-gray-900">
                      Also clear MSA tracking + user schedules
                    </span>
                    <span className="block text-gray-500">
                      Includes <code>MSA_Calculation_Sequence</code>,{' '}
                      <code>MSA_Column_Definitions</code>, and{' '}
                      <code>ARS_PEND_ALC_SCHEDULE</code>.
                    </span>
                  </span>
                </label>
              </div>

              {/* Preview */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="font-medium text-gray-800 flex items-center gap-2">
                    <Eye size={16} /> Preview — what would be cleared
                  </h4>
                  <button
                    onClick={() => loadResetPreview(resetIncludeMsa)}
                    className="btn-secondary text-xs"
                    disabled={resetPreviewLoading}
                  >
                    <RefreshCw size={14} className={resetPreviewLoading ? 'animate-spin' : ''} />
                    Refresh
                  </button>
                </div>

                {resetPreviewLoading && (
                  <div className="text-sm text-gray-500 py-4 text-center">Loading preview…</div>
                )}

                {!resetPreviewLoading && resetPreview && (
                  <div className="space-y-3">
                    <div className="grid grid-cols-3 gap-3">
                      <div className="p-3 bg-red-50 rounded-lg">
                        <div className="text-xs text-red-700">Tables to clear</div>
                        <div className="text-2xl font-bold text-red-700">
                          {resetPreview.totals?.cleared || 0}
                        </div>
                      </div>
                      <div className="p-3 bg-red-50 rounded-lg">
                        <div className="text-xs text-red-700">Rows to delete</div>
                        <div className="text-2xl font-bold text-red-700">
                          {(resetPreview.totals?.rows_deleted || 0).toLocaleString()}
                        </div>
                      </div>
                      <div className="p-3 bg-gray-50 rounded-lg">
                        <div className="text-xs text-gray-600">Skipped (preserved)</div>
                        <div className="text-2xl font-bold text-gray-700">
                          {resetPreview.totals?.skipped || 0}
                        </div>
                      </div>
                    </div>

                    <div className="max-h-72 overflow-y-auto border rounded-lg">
                      <table className="w-full text-sm">
                        <thead className="bg-gray-50 sticky top-0">
                          <tr className="text-left text-xs text-gray-600 uppercase">
                            <th className="px-3 py-2">DB</th>
                            <th className="px-3 py-2">Table</th>
                            <th className="px-3 py-2">Method</th>
                            <th className="px-3 py-2 text-right">Rows</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(resetPreview.cleared || []).map((r, i) => (
                            <tr key={i} className="border-t">
                              <td className="px-3 py-1.5 text-gray-500">{r.db}</td>
                              <td className="px-3 py-1.5 font-mono text-xs">{r.table}</td>
                              <td className="px-3 py-1.5">
                                <span className={`text-xs font-medium px-2 py-0.5 rounded ${
                                  r.method?.includes('TRUNCATE') ? 'bg-orange-100 text-orange-800'
                                  : 'bg-yellow-100 text-yellow-800'
                                }`}>
                                  {r.method?.replace('WOULD_', '')}
                                </span>
                              </td>
                              <td className="px-3 py-1.5 text-right font-mono">
                                {(r.rows_before || 0).toLocaleString()}
                              </td>
                            </tr>
                          ))}
                          {(resetPreview.cleared || []).length === 0 && (
                            <tr>
                              <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                                Nothing to clear — already at zero state.
                              </td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>

              {/* Confirm + Execute */}
              <div className="border-t pt-4 space-y-3">
                <label className="block text-sm">
                  <span className="font-medium text-red-700">
                    Type <code className="px-1.5 py-0.5 bg-red-100 rounded">RESET</code> to confirm:
                  </span>
                  <input
                    type="text"
                    value={resetConfirmText}
                    onChange={e => setResetConfirmText(e.target.value)}
                    placeholder="RESET"
                    className="input mt-2 w-full max-w-xs font-mono"
                    disabled={resetRunning}
                  />
                </label>

                <button
                  onClick={runTransactionalReset}
                  disabled={
                    resetRunning ||
                    resetConfirmText !== 'RESET' ||
                    !(resetPreview?.totals?.cleared)
                  }
                  className="px-5 py-2.5 rounded-lg bg-red-600 text-white font-medium
                             hover:bg-red-700 disabled:bg-gray-300 disabled:cursor-not-allowed
                             flex items-center gap-2"
                >
                  {resetRunning ? <RefreshCw size={16} className="animate-spin" /> : <Trash2 size={16} />}
                  {resetRunning ? 'Resetting…' : 'Wipe Transactional Data'}
                </button>
              </div>

              {/* Last run report */}
              {resetReport && (
                <div className="border-t pt-4">
                  <h4 className="font-medium text-gray-800 mb-2 flex items-center gap-2">
                    <Check size={16} className="text-green-600" /> Last reset
                  </h4>
                  <div className="text-sm space-y-1 text-gray-700">
                    <div>Cleared: <strong>{resetReport.totals?.cleared}</strong> table(s)</div>
                    <div>Rows deleted: <strong>{(resetReport.totals?.rows_deleted || 0).toLocaleString()}</strong></div>
                    <div>Errors: <strong className={resetReport.totals?.errors ? 'text-red-700' : ''}>
                      {resetReport.totals?.errors || 0}
                    </strong></div>
                  </div>
                  {resetReport.errors?.length > 0 && (
                    <div className="mt-2 p-3 bg-red-50 rounded text-xs text-red-800 space-y-1">
                      {resetReport.errors.map((e, i) => (
                        <div key={i}><strong>{e.table}:</strong> {e.error}</div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}


function TablePermissionsTab() {
  const [tables, setTables] = useState([])
  const [permissions, setPermissions] = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    loadData()
  }, [])

  const loadData = async () => {
    setLoading(true)
    try {
      const [tablesRes, permsRes] = await Promise.all([
        tablesAPI.listAll(),
        tablesAPI.tablePermissions()
      ])
      
      const allTables = tablesRes.data.data || []
      const existingPerms = permsRes.data.data || []
      
      // Create permissions array with all tables
      const permsMap = {}
      existingPerms.forEach(p => {
        permsMap[p.table_name] = p
      })
      
      const fullPerms = allTables.map(t => ({
        table_name: t.table_name,
        can_view: permsMap[t.table_name]?.can_view ?? true,
        can_edit: permsMap[t.table_name]?.can_edit ?? false,
        can_upload: permsMap[t.table_name]?.can_upload ?? false,
        can_export: permsMap[t.table_name]?.can_export ?? false,
        can_delete: permsMap[t.table_name]?.can_delete ?? false,
      }))
      
      setTables(allTables)
      setPermissions(fullPerms)
    } catch (err) {
      toast.error('Failed to load table permissions')
    } finally {
      setLoading(false)
    }
  }

  const updatePermission = (tableName, field, value) => {
    setPermissions(prev => prev.map(p => 
      p.table_name === tableName ? { ...p, [field]: value } : p
    ))
  }

  const toggleAll = (field, value) => {
    setPermissions(prev => prev.map(p => ({ ...p, [field]: value })))
  }

  const savePermissions = async () => {
    setSaving(true)
    try {
      await tablesAPI.saveTablePermissions(permissions)
      toast.success('Table permissions saved')
    } catch {
      toast.error('Failed to save permissions')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="card p-6">
        <div className="flex items-center justify-center py-12 text-gray-400">
          <RefreshCw className="animate-spin mr-2" size={20} />
          Loading tables...
        </div>
      </div>
    )
  }

  return (
    <div className="card p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="font-semibold text-gray-900 text-lg flex items-center gap-2">
            <Table2 size={20} /> Table Permissions
          </h3>
          <p className="text-sm text-gray-500 mt-1">Control which tables can be viewed, edited, uploaded, exported, or deleted</p>
        </div>
        <button onClick={savePermissions} disabled={saving} className="btn-primary">
          {saving ? <RefreshCw size={14} className="animate-spin" /> : <Save size={14} />}
          {saving ? 'Saving...' : 'Save Permissions'}
        </button>
      </div>

      <div className="overflow-auto max-h-[500px] border rounded-lg">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 sticky top-0">
            <tr>
              <th className="px-4 py-3 text-left font-medium text-gray-700">Table Name</th>
              <th className="px-3 py-3 text-center font-medium text-gray-700 w-20">
                <div className="flex flex-col items-center">
                  <Eye size={14} className="mb-1" />
                  <span>View</span>
                  <button onClick={() => toggleAll('can_view', !permissions.every(p => p.can_view))} className="text-xs text-primary-600 hover:underline">all</button>
                </div>
              </th>
              <th className="px-3 py-3 text-center font-medium text-gray-700 w-20">
                <div className="flex flex-col items-center">
                  <Edit3 size={14} className="mb-1" />
                  <span>Edit</span>
                  <button onClick={() => toggleAll('can_edit', !permissions.every(p => p.can_edit))} className="text-xs text-primary-600 hover:underline">all</button>
                </div>
              </th>
              <th className="px-3 py-3 text-center font-medium text-gray-700 w-20">
                <div className="flex flex-col items-center">
                  <Upload size={14} className="mb-1" />
                  <span>Upload</span>
                  <button onClick={() => toggleAll('can_upload', !permissions.every(p => p.can_upload))} className="text-xs text-primary-600 hover:underline">all</button>
                </div>
              </th>
              <th className="px-3 py-3 text-center font-medium text-gray-700 w-20">
                <div className="flex flex-col items-center">
                  <FileDown size={14} className="mb-1" />
                  <span>Export</span>
                  <button onClick={() => toggleAll('can_export', !permissions.every(p => p.can_export))} className="text-xs text-primary-600 hover:underline">all</button>
                </div>
              </th>
              <th className="px-3 py-3 text-center font-medium text-gray-700 w-20">
                <div className="flex flex-col items-center">
                  <Trash2 size={14} className="mb-1" />
                  <span>Delete</span>
                  <button onClick={() => toggleAll('can_delete', !permissions.every(p => p.can_delete))} className="text-xs text-primary-600 hover:underline">all</button>
                </div>
              </th>
            </tr>
          </thead>
          <tbody>
            {permissions.map((perm, idx) => (
              <tr key={perm.table_name} className={idx % 2 === 0 ? 'bg-white' : 'bg-gray-50'}>
                <td className="px-4 py-2 font-medium text-gray-900">{perm.table_name}</td>
                <td className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    checked={perm.can_view}
                    onChange={e => updatePermission(perm.table_name, 'can_view', e.target.checked)}
                    className="w-4 h-4 rounded text-primary-600"
                  />
                </td>
                <td className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    checked={perm.can_edit}
                    onChange={e => updatePermission(perm.table_name, 'can_edit', e.target.checked)}
                    className="w-4 h-4 rounded text-blue-600"
                  />
                </td>
                <td className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    checked={perm.can_upload}
                    onChange={e => updatePermission(perm.table_name, 'can_upload', e.target.checked)}
                    className="w-4 h-4 rounded text-green-600"
                  />
                </td>
                <td className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    checked={perm.can_export}
                    onChange={e => updatePermission(perm.table_name, 'can_export', e.target.checked)}
                    className="w-4 h-4 rounded text-amber-600"
                  />
                </td>
                <td className="px-3 py-2 text-center">
                  <input
                    type="checkbox"
                    checked={perm.can_delete}
                    onChange={e => updatePermission(perm.table_name, 'can_delete', e.target.checked)}
                    className="w-4 h-4 rounded text-red-600"
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-gray-500">
        <strong>Note:</strong> System columns (id, created_at, updated_at, etc.) are automatically excluded from upload templates and cannot be modified. 
        Tables without Upload permission will not appear in the Upload Data page dropdown.
      </p>
    </div>
  )
}
