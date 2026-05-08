import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { 
  Upload, Download, Database, Clock, CheckCircle2, XCircle, 
  AlertCircle, RefreshCw, BarChart3, Activity, FileText, 
  TrendingUp, Users, Table2, Zap
} from 'lucide-react'
import { uploadAPI, auditAPI } from '@/services/api'
import api from '@/services/api'
import toast from 'react-hot-toast'
import { format, formatDistanceToNow } from 'date-fns'

export default function JobsDashboardPage() {
  const [uploadJobs, setUploadJobs] = useState([])
  const [exportJobs, setExportJobs] = useState([])
  const [msaStorageJobs, setMsaStorageJobs] = useState([])
  const [recentAudit, setRecentAudit] = useState([])
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    loadAll()
    const interval = setInterval(loadAll, 10000) // Refresh every 10s
    return () => clearInterval(interval)
  }, [])

  const loadAll = async () => {
    try {
      console.log('🔄 Loading all dashboard data...')
      const [uploadsRes, exportsRes, msaRes, auditRes, statsRes] = await Promise.all([
        uploadAPI.listAllJobs().catch(err => { console.error('Upload jobs error:', err); return { data: { data: [] } } }),
        api.get('/tables/export/jobs').catch(err => { console.error('Export jobs error:', err); return { data: { data: [] } } }),
        api.get('/msa/jobs').catch(err => { console.error('MSA jobs error:', err); return { data: { data: { jobs: [] } } } }),
        auditAPI.list({ page_size: 20 }).catch(err => { console.error('Audit error:', err); return { data: { data: { logs: [] } } } }),
        api.get('/dashboard/stats').catch(err => { console.error('Stats error:', err); return { data: { data: null } } }),
      ])
      
      console.log('✅ API Responses received:')
      console.log('  Uploads:', uploadsRes.data?.data)
      console.log('  Exports:', exportsRes.data?.data)
      console.log('  MSA Jobs:', msaRes.data?.data)
      console.log('  MSA Job statuses:', msaRes.data?.data?.jobs?.map(j => ({ id: j.job_id, status: j.status })))
      console.log('  Audit:', auditRes.data?.data?.logs?.length, 'logs')
      console.log('  Stats:', statsRes.data?.data)
      
      setUploadJobs(uploadsRes.data?.data || [])
      setExportJobs(exportsRes.data?.data || [])
      setMsaStorageJobs(msaRes.data?.data?.jobs || [])
      setRecentAudit(auditRes.data?.data?.logs || [])
      setStats(statsRes.data?.data)
      
      console.log('✅ State updated with', msaRes.data?.data?.jobs?.length || 0, 'MSA jobs')
    } catch (err) {
      console.error('Failed to load dashboard data:', err)
    } finally {
      setLoading(false)
    }
  }

  const getStatusBadge = (status) => {
    const styles = {
      pending: 'bg-yellow-100 text-yellow-700',
      running: 'bg-blue-100 text-blue-700',
      completed: 'bg-green-100 text-green-700',
      failed: 'bg-red-100 text-red-700',
      cancelled: 'bg-gray-100 text-gray-700',
    }
    const icons = {
      pending: <Clock size={12} />,
      running: <Activity size={12} className="animate-pulse" />,
      completed: <CheckCircle2 size={12} />,
      failed: <XCircle size={12} />,
      cancelled: <AlertCircle size={12} />,
    }
    return (
      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${styles[status] || styles.pending}`}>
        {icons[status]} {status}
      </span>
    )
  }

  const formatNumber = (num) => {
    if (!num) return '0'
    if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M'
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K'
    return num.toLocaleString()
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="animate-spin text-primary-500" size={32} />
      </div>
    )
  }

  // Count jobs by status
  const uploadsByStatus = uploadJobs.reduce((acc, j) => {
    acc[j.status] = (acc[j.status] || 0) + 1
    return acc
  }, {})
  const exportsByStatus = exportJobs.reduce((acc, j) => {
    acc[j.status] = (acc[j.status] || 0) + 1
    return acc
  }, {})
  const msaByStatus = msaStorageJobs.reduce((acc, j) => {
    acc[j.status] = (acc[j.status] || 0) + 1
    return acc
  }, {})

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Jobs Dashboard</h1>
          <p className="text-gray-500 text-sm mt-0.5">Monitor all upload, export and data operations</p>
        </div>
        <button onClick={loadAll} className="btn-secondary">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <StatsCard
          icon={<Upload className="text-blue-500" />}
          title="Upload Jobs"
          value={uploadJobs.length}
          subtitle={`${uploadsByStatus.running || 0} running, ${uploadsByStatus.completed || 0} completed`}
          color="blue"
          to="/upload"
        />
        <StatsCard
          icon={<Download className="text-green-500" />}
          title="Export Jobs"
          value={exportJobs.length}
          subtitle={`${exportsByStatus.running || 0} running, ${exportsByStatus.completed || 0} completed`}
          color="green"
          to="/export"
        />
        <StatsCard
          icon={<TrendingUp className="text-indigo-500" />}
          title="MSA Storage Jobs"
          value={msaStorageJobs.length}
          subtitle={`${msaByStatus.running || 0} running, ${msaByStatus.completed || 0} completed`}
          color="indigo"
          to="/msa"
        />
        <StatsCard
          icon={<FileText className="text-purple-500" />}
          title="Audit Entries"
          value={formatNumber(stats?.total_audit_logs || recentAudit.length)}
          subtitle="Last 24 hours"
          color="purple"
          to="/settings/audit"
        />
        <StatsCard
          icon={<Database className="text-orange-500" />}
          title="Tables"
          value={stats?.total_tables || '-'}
          subtitle={`${formatNumber(stats?.total_rows || 0)} total rows`}
          color="orange"
          to="/tables"
        />
      </div>

      {/* Main Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Upload Jobs */}
        <div className="card">
          <div className="card-header flex items-center justify-between">
            <h2 className="font-semibold flex items-center gap-2">
              <Upload size={16} className="text-blue-500" /> Recent Upload Jobs
            </h2>
            <Link to="/data/upload" className="text-xs text-primary-600 hover:text-primary-700">
              View All →
            </Link>
          </div>
          <div className="divide-y max-h-80 overflow-auto">
            {uploadJobs.length === 0 ? (
              <div className="p-8 text-center text-gray-400">No upload jobs</div>
            ) : (
              uploadJobs.slice(0, 10).map(job => (
                <div key={job.job_id} className="p-3 hover:bg-gray-50">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-sm truncate">{job.table_name}</span>
                        {getStatusBadge(job.status)}
                      </div>
                      <div className="text-xs text-gray-500 mt-1">
                        {job.file_name} • {formatNumber(job.total_rows)} rows
                      </div>
                      {job.status === 'running' && job.total_rows > 0 && (
                        <div className="mt-2">
                          <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
                            <span>{formatNumber(job.processed_rows)} / {formatNumber(job.total_rows)}</span>
                            <span>{Math.round((job.processed_rows / job.total_rows) * 100)}%</span>
                          </div>
                          <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden">
                            <div 
                              className="h-full bg-blue-500 transition-all duration-500"
                              style={{ width: `${(job.processed_rows / job.total_rows) * 100}%` }}
                            />
                          </div>
                        </div>
                      )}
                      {job.status === 'completed' && (
                        <div className="text-xs text-gray-500 mt-1">
                          <span className="text-green-600">+{formatNumber(job.inserted_rows)} inserted</span>
                          {job.updated_rows > 0 && <span className="text-blue-600 ml-2">~{formatNumber(job.updated_rows)} updated</span>}
                        </div>
                      )}
                      {job.status === 'failed' && job.error_message && (
                        <div className="text-xs text-red-600 mt-1 truncate">{job.error_message}</div>
                      )}
                    </div>
                    <div className="flex flex-col items-end gap-1 shrink-0">
                      <div className="text-xs text-gray-400">
                        {job.created_at ? formatDistanceToNow(new Date(job.created_at), { addSuffix: true }) : ''}
                      </div>
                      {(job.status === 'running' || job.status === 'queued') && (
                        <button onClick={async () => {
                          try {
                            await api.post(`/upload/jobs/${job.job_id}/cancel`, null, { params: { force: true } })
                            toast.success('Job cancelled')
                            loadAll()
                          } catch { toast.error('Failed to cancel') }
                        }} className="text-[10px] text-red-500 hover:text-red-700 font-medium flex items-center gap-0.5">
                          <XCircle size={10}/> Stop
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Export Jobs */}
        <div className="card">
          <div className="card-header flex items-center justify-between">
            <h2 className="font-semibold flex items-center gap-2">
              <Download size={16} className="text-green-500" /> Recent Export Jobs
            </h2>
            <Link to="/data/export" className="text-xs text-primary-600 hover:text-primary-700">
              View All →
            </Link>
          </div>
          <div className="divide-y max-h-80 overflow-auto">
            {exportJobs.length === 0 ? (
              <div className="p-8 text-center text-gray-400">No export jobs</div>
            ) : (
              exportJobs.slice(0, 10).map(job => (
                <div key={job.job_id} className="p-3 hover:bg-gray-50">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-sm truncate">{job.table_name}</span>
                        {getStatusBadge(job.status)}
                      </div>
                      <div className="text-xs text-gray-500 mt-1">
                        {job.format?.toUpperCase()} • {formatNumber(job.total_rows)} rows
                        {job.file_size && ` • ${(job.file_size / 1024 / 1024).toFixed(1)} MB`}
                      </div>
                      {job.status === 'completed' && job.downloaded > 0 && (
                        <div className="text-xs text-gray-500 mt-1">
                          Downloaded {job.downloaded} time(s)
                        </div>
                      )}
                    </div>
                    <div className="flex flex-col items-end gap-1 shrink-0">
                      <div className="text-xs text-gray-400">
                        {job.created_at ? formatDistanceToNow(new Date(job.created_at), { addSuffix: true }) : ''}
                      </div>
                      {job.status === 'running' && (
                        <button onClick={async () => {
                          try {
                            await api.delete(`/tables/export/jobs/${job.job_id}`)
                            toast.success('Export job deleted')
                            loadAll()
                          } catch { toast.error('Failed to delete') }
                        }} className="text-[10px] text-red-500 hover:text-red-700 font-medium flex items-center gap-0.5">
                          <XCircle size={10}/> Stop
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* MSA Storage Jobs */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h2 className="font-semibold flex items-center gap-2">
            <TrendingUp size={16} className="text-indigo-500" /> MSA Storage Jobs
          </h2>
          {msaStorageJobs.some(j => j.status === 'pending' || j.status === 'queued') && (
            <button
              onClick={() => {
                if (window.confirm('Cancel all pending MSA jobs?')) {
                  api.post('/msa/jobs/cancel/all')
                    .then(() => {
                      toast.success('Cancelled all pending jobs')
                      loadAll()
                    })
                    .catch(err => {
                      console.error('Cancel all error:', err)
                      toast.error('Failed to cancel jobs')
                    })
                }
              }}
              className="px-3 py-1 bg-red-500 hover:bg-red-600 text-white text-xs rounded transition-colors"
            >
              Cancel All
            </button>
          )}
        </div>
        <div className="divide-y max-h-96 overflow-auto">
          {msaStorageJobs.length === 0 ? (
            <div className="p-8 text-center text-gray-400">No MSA storage jobs</div>
          ) : (
            msaStorageJobs.slice(0, 20).map(job => (
              <div key={job.job_id} className="p-3 hover:bg-gray-50">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm">Seq #{job.sequence_id}</span>
                      {getStatusBadge(job.status)}
                      <span className="text-xs text-gray-400 font-mono">{job.job_id}</span>
                    </div>
                    <div className="text-xs text-gray-500 mt-1">
                      {formatNumber(job.total_rows)} total rows
                      {job.inserted_msa > 0 && ` • MSA: ${formatNumber(job.inserted_msa)}`}
                      {job.inserted_colors > 0 && ` • Colors: ${formatNumber(job.inserted_colors)}`}
                      {job.inserted_variants > 0 && ` • Variants: ${formatNumber(job.inserted_variants)}`}
                    </div>
                    {job.status === 'running' && job.total_rows > 0 && (
                      <div className="mt-2">
                        <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
                          <span>{formatNumber(job.processed_rows)} / {formatNumber(job.total_rows)}</span>
                          <span>{Math.round((job.processed_rows / job.total_rows) * 100)}%</span>
                        </div>
                        <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden">
                          <div 
                            className="h-full bg-indigo-500 transition-all duration-500"
                            style={{ width: `${(job.processed_rows / job.total_rows) * 100}%` }}
                          />
                        </div>
                      </div>
                    )}
                    {job.status === 'completed' && job.duration_ms && (
                      <div className="text-xs text-gray-500 mt-1">
                        Completed in {(job.duration_ms / 1000).toFixed(2)}s
                      </div>
                    )}
                    {job.status === 'failed' && job.error_message && (
                      <div className="text-xs text-red-600 mt-1 truncate">{job.error_message}</div>
                    )}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {(job.status === 'pending' || job.status === 'queued') && (
                      <button
                        onClick={() => {
                          api.post(`/msa/jobs/${job.job_id}/cancel`)
                            .then(() => {
                              toast.success(`Job ${job.job_id.slice(-4)} cancelled`)
                              loadAll()
                            })
                            .catch(err => {
                              console.error('Cancel error:', err)
                              toast.error('Failed to cancel job')
                            })
                        }}
                        className="px-2 py-1 bg-red-500 hover:bg-red-600 text-white text-xs rounded transition-colors"
                      >
                        Cancel
                      </button>
                    )}
                    <span className="text-xs text-gray-400">
                      {job.created_at ? formatDistanceToNow(new Date(job.created_at), { addSuffix: true }) : ''}
                    </span>
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Recent Audit Activity */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h2 className="font-semibold flex items-center gap-2">
            <Activity size={16} className="text-purple-500" /> Recent Audit Activity
          </h2>
          <Link to="/settings/audit" className="text-xs text-primary-600 hover:text-primary-700">
            View All →
          </Link>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-xs text-gray-500 uppercase">
              <tr>
                <th className="px-4 py-2 text-left">Table</th>
                <th className="px-4 py-2 text-left">Action</th>
                <th className="px-4 py-2 text-left">Changed By</th>
                <th className="px-4 py-2 text-left">Rows</th>
                <th className="px-4 py-2 text-left">Source</th>
                <th className="px-4 py-2 text-left">Time</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {recentAudit.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-gray-400">No audit logs</td>
                </tr>
              ) : (
                recentAudit.map(log => (
                  <tr key={log.id} className="hover:bg-gray-50">
                    <td className="px-4 py-2 font-medium">{log.table_name}</td>
                    <td className="px-4 py-2">
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                        log.action_type === 'INSERT' || log.action_type === 'BULK_UPSERT' ? 'bg-green-100 text-green-700' :
                        log.action_type === 'UPDATE' ? 'bg-blue-100 text-blue-700' :
                        log.action_type === 'DELETE' ? 'bg-red-100 text-red-700' :
                        'bg-gray-100 text-gray-700'
                      }`}>
                        {log.action_type}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-gray-600">{log.changed_by}</td>
                    <td className="px-4 py-2 text-gray-600">{formatNumber(log.row_count)}</td>
                    <td className="px-4 py-2">
                      <span className={`px-2 py-0.5 rounded text-xs ${
                        log.source === 'UPLOAD' ? 'bg-blue-50 text-blue-600' :
                        log.source === 'UI' ? 'bg-purple-50 text-purple-600' :
                        'bg-gray-50 text-gray-600'
                      }`}>
                        {log.source}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-gray-500 text-xs">
                      {log.changed_at ? formatDistanceToNow(new Date(log.changed_at), { addSuffix: true }) : ''}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function StatsCard({ icon, title, value, subtitle, color, to }) {
  const colors = {
    blue: 'from-blue-50 to-blue-100 border-blue-200',
    green: 'from-green-50 to-green-100 border-green-200',
    indigo: 'from-indigo-50 to-indigo-100 border-indigo-200',
    purple: 'from-purple-50 to-purple-100 border-purple-200',
    orange: 'from-orange-50 to-orange-100 border-orange-200',
  }
  const Wrapper = to ? Link : 'div'
  const wrapperProps = to ? { to } : {}
  return (
    <Wrapper {...wrapperProps} className={`block p-3 rounded-xl bg-gradient-to-br ${colors[color]} border hover:shadow-md transition-shadow cursor-pointer group`}>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-[10px] text-gray-500 font-semibold">{title}</p>
          <p className="text-xl font-bold text-gray-900 mt-0.5">{value}</p>
          <p className="text-[10px] text-gray-500 mt-0.5">{subtitle}</p>
        </div>
        <div className="p-1.5 bg-white rounded-lg shadow-sm group-hover:shadow-md transition-shadow">
          {icon}
        </div>
      </div>
    </Wrapper>
  )
}
