/**
 * AutoContJobsPage — Auto Cont % (SQL-direct pipeline)
 * Live list of background jobs with status, progress, cancel, delete, and
 * a deep-link to Review for completed jobs.
 */
import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { autoContAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Activity, RefreshCw, X, Trash2, Eye, AlertTriangle, CheckCircle,
  Loader2, Clock,
} from 'lucide-react'
import { C } from '@/theme/colors'

const statusColor = (s) => ({
  pending:   '#64748b',
  running:   C.primary,
  completed: C.green,
  failed:    C.red,
  cancelled: C.amber,
})[s] || C.textMuted

const statusIcon = (s, size = 12) => {
  const Style = { color: statusColor(s) }
  switch (s) {
    case 'running':   return <Loader2 size={size} style={Style} className="spin" />
    case 'completed': return <CheckCircle size={size} style={Style} />
    case 'failed':    return <AlertTriangle size={size} style={Style} />
    case 'cancelled': return <X size={size} style={Style} />
    default:          return <Clock size={size} style={Style} />
  }
}

export default function AutoContJobsPage() {
  const nav = useNavigate()
  const [jobs, setJobs] = useState([])
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState(null)
  const pollRef = useRef(null)

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await autoContAPI.listJobs()
      setJobs(data?.data?.jobs || [])
    } catch { toast.error('Failed to load jobs') }
    finally { setLoading(false) }
  }

  useEffect(() => {
    load()
    // Re-poll every 3s while there are active jobs
    pollRef.current = setInterval(load, 3000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  const cancel = async (id) => {
    try { await autoContAPI.cancelJob(id); toast('Cancel requested'); load() }
    catch { toast.error('Cancel failed') }
  }

  const remove = async (id) => {
    if (!confirm(`Delete job ${id}?`)) return
    try { await autoContAPI.deleteJob(id); toast.success('Deleted'); load() }
    catch { toast.error('Delete failed') }
  }

  const activeCount = jobs.filter(j => ['pending','running'].includes(j.status)).length

  return (
    <div style={{ color: C.text }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h1 style={{ fontSize: 20, fontWeight: 800, margin: 0, display: 'flex', alignItems: 'center', gap: 10 }}>
          <Activity size={20} color={C.primary} /> Auto Cont % — Jobs
          {activeCount > 0 && (
            <span style={{
              fontSize: 11, padding: '2px 10px', borderRadius: 10,
              background: C.primary, color: '#fff', marginLeft: 6,
            }}>{activeCount} active</span>
          )}
        </h1>
        <button onClick={load} style={{
          padding: '6px 14px', borderRadius: 6, fontSize: 12, fontWeight: 600,
          border: `1px solid ${C.cardBorder}`, background: '#fff', color: C.textSub,
          cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
        }}>
          <RefreshCw size={12} className={loading ? 'spin' : ''} /> Refresh
        </button>
      </div>

      <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ background: C.headerBg, borderBottom: `1px solid ${C.cardBorder}` }}>
              <th style={th}>Status</th>
              <th style={th}>Job ID</th>
              <th style={th}>Label</th>
              <th style={th}>Progress</th>
              <th style={{ ...th, textAlign: 'right' }}>Detail</th>
              <th style={{ ...th, textAlign: 'right' }}>Company</th>
              <th style={{ ...th, textAlign: 'right' }}>Duration</th>
              <th style={th}>Created</th>
              <th style={th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {jobs.length === 0 && (
              <tr><td colSpan={9} style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>
                No jobs yet. Run one from <b>Execute</b>.
              </td></tr>
            )}
            {jobs.map(j => (
              <>
                <tr key={j.id}
                    onClick={() => setExpanded(expanded === j.id ? null : j.id)}
                    style={{ borderBottom: `1px solid ${C.cardBorder}`, cursor: 'pointer',
                             background: expanded === j.id ? C.primaryLight : '#fff' }}>
                  <td style={td}>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700, color: statusColor(j.status), textTransform: 'capitalize' }}>
                      {statusIcon(j.status)} {j.status}
                    </span>
                  </td>
                  <td style={{ ...td, fontFamily: 'monospace' }}>{j.id}</td>
                  <td style={td}>{j.label}</td>
                  <td style={{ ...td, color: C.textMuted }}>{j.progress || '—'}</td>
                  <td style={{ ...td, textAlign: 'right' }}>{(j.detail_rows || 0).toLocaleString()}</td>
                  <td style={{ ...td, textAlign: 'right' }}>{(j.company_rows || 0).toLocaleString()}</td>
                  <td style={{ ...td, textAlign: 'right' }}>{j.duration ? `${j.duration}s` : '—'}</td>
                  <td style={{ ...td, color: C.textMuted }}>{fmtTime(j.created_at)}</td>
                  <td style={td} onClick={e => e.stopPropagation()}>
                    <div style={{ display: 'flex', gap: 6 }}>
                      {['pending','running'].includes(j.status) && (
                        <button onClick={() => cancel(j.id)} title="Cancel" style={iconBtn(C.red)}>
                          <X size={12} />
                        </button>
                      )}
                      {j.status === 'completed' && (j.detail_table || j.company_table) && (
                        <button onClick={() => nav('/auto-cont/review')} title="View results" style={iconBtn(C.green)}>
                          <Eye size={12} />
                        </button>
                      )}
                      <button onClick={() => remove(j.id)} title="Delete" style={iconBtn(C.textMuted)}>
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </td>
                </tr>
                {expanded === j.id && (
                  <tr style={{ background: '#f8fafc' }}>
                    <td colSpan={9} style={{ padding: '12px 16px' }}>
                      {/* Output tables */}
                      <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: 6, fontSize: 11, marginBottom: 10 }}>
                        <div style={{ color: C.textMuted }}>Detail table</div>
                        <div style={{ fontFamily: 'monospace' }}>{j.detail_table || '—'}</div>
                        <div style={{ color: C.textMuted }}>Company table</div>
                        <div style={{ fontFamily: 'monospace' }}>{j.company_table || '—'}</div>
                        <div style={{ color: C.textMuted }}>Started</div>
                        <div>{fmtTime(j.started_at)}</div>
                        <div style={{ color: C.textMuted }}>Finished</div>
                        <div>{fmtTime(j.finished_at)}</div>
                      </div>
                      {j.error && (
                        <div style={{
                          padding: 10, borderRadius: 6,
                          background: '#fef2f2', border: '1px solid #fecaca', color: '#991b1b',
                          fontSize: 11, fontFamily: 'monospace', whiteSpace: 'pre-wrap',
                        }}>{j.error}</div>
                      )}
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </div>

      <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}

const th = { padding: '10px 12px', textAlign: 'left', fontWeight: 700, fontSize: 11,
             color: C.textSub, textTransform: 'uppercase', letterSpacing: 0.4 }
const td = { padding: '10px 12px', verticalAlign: 'middle' }
const iconBtn = (color) => ({
  background: 'none', border: 'none', cursor: 'pointer', padding: 4,
  color, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  borderRadius: 4,
})

function fmtTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch { return iso }
}
