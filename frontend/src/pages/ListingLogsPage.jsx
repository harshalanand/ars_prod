/**
 * ListingLogsPage — review prior /listing/generate runs.
 *
 * Reads ARS_LISTING_SESSIONS for the table on the left and the per-session
 * log file (logs/listing_sessions/<sid>.log on the backend) for the viewer
 * on the right. No data is mutated here — pure read-only diagnostics.
 */
import { useEffect, useMemo, useState } from 'react'
import { listingAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { ChevronLeft, RefreshCw, FileText, Activity, Search, Filter, Square, Trash2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { C } from '@/theme/colors'

const STATUS_COLOR = {
  RUNNING: '#3b82f6',
  SUCCESS: '#10b981',
  FAILED:  '#dc2626',
}

function fmt(d) {
  if (!d) return '—'
  try { return new Date(d).toLocaleString() } catch { return d }
}

function fmtSec(s) {
  if (s == null) return '—'
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60); const r = (s - m * 60).toFixed(0)
  return `${m}m ${r}s`
}

export default function ListingLogsPage() {
  const navigate = useNavigate()

  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(false)
  const [filterStatus, setFilterStatus] = useState('')
  const [filterMode, setFilterMode] = useState('')
  const [search, setSearch] = useState('')

  const [selectedId, setSelectedId] = useState(null)
  const [selectedMeta, setSelectedMeta] = useState(null)
  const [logText, setLogText] = useState('')
  const [logLoading, setLogLoading] = useState(false)
  const [tailMode, setTailMode] = useState(false)        // true => last 500 lines

  const loadSessions = async () => {
    setLoading(true)
    try {
      const params = { limit: 100 }
      if (filterStatus) params.status = filterStatus
      if (filterMode)   params.mode = filterMode
      const { data } = await listingAPI.sessions(params)
      setSessions(data?.sessions || [])
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load sessions')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { loadSessions() }, [filterStatus, filterMode])

  const filtered = useMemo(() => {
    if (!search) return sessions
    const s = search.toLowerCase()
    return sessions.filter(x =>
      (x.session_id || '').toLowerCase().includes(s) ||
      (x.user || '').toLowerCase().includes(s) ||
      (x.allocation_mode || '').toLowerCase().includes(s) ||
      (x.error_msg || '').toLowerCase().includes(s)
    )
  }, [sessions, search])

  const loadSelected = async (sid) => {
    setSelectedId(sid)
    setSelectedMeta(null)
    setLogText('')
    setLogLoading(true)
    try {
      const [m, l] = await Promise.all([
        listingAPI.session(sid),
        listingAPI.sessionLog(sid, tailMode ? 500 : null),
      ])
      setSelectedMeta(m?.data?.session || null)
      setLogText(l?.data?.log || '(empty log)')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load session')
      setLogText(`Error: ${e.response?.data?.detail || e.message}`)
    } finally {
      setLogLoading(false)
    }
  }

  // Re-fetch the log when user toggles tail mode and a session is selected.
  useEffect(() => { if (selectedId) loadSelected(selectedId) }, [tailMode])

  const [busyId, setBusyId] = useState(null)

  const handleKill = async (sid, e) => {
    e?.stopPropagation()
    if (!window.confirm(`Kill session ${sid}?\n\nThis marks the session FAILED and cancels its in-flight allocation queue rows. The Python thread itself can't be preempted but its bookkeeping will be closed.`)) return
    setBusyId(sid)
    try {
      const { data } = await listingAPI.killSession(sid)
      toast.success(`Killed · ${data.queue_rows_cancelled || 0} queue rows cancelled`)
      await loadSessions()
      if (selectedId === sid) await loadSelected(sid)
    } catch (e2) {
      toast.error(e2.response?.data?.detail || 'Kill failed')
    } finally {
      setBusyId(null)
    }
  }

  const handleDelete = async (sid, e) => {
    e?.stopPropagation()
    if (!window.confirm(`Permanently delete session ${sid}?\n\nThis removes the session row and its log file. Cannot be undone.`)) return
    setBusyId(sid)
    try {
      await listingAPI.deleteSession(sid)
      toast.success('Session deleted')
      if (selectedId === sid) {
        setSelectedId(null); setSelectedMeta(null); setLogText('')
      }
      await loadSessions()
    } catch (e2) {
      toast.error(e2.response?.data?.detail || 'Delete failed')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div style={{ color: C.text, fontFamily: 'inherit', display: 'flex',
                  flexDirection: 'column', gap: 10, padding: '4px 2px' }}>

      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        background: 'linear-gradient(135deg, #fff 0%, #f8fafc 100%)',
        border: `1px solid ${C.cardBorder}`, borderRadius: 10, padding: '10px 14px',
        boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <button onClick={() => navigate('/data-prep/listing')}
            style={{ height: 28, padding: '0 10px', borderRadius: 6,
              background: '#fff', border: `1px solid ${C.cardBorder}`,
              cursor: 'pointer', fontSize: 11, fontWeight: 600, color: C.text,
              display: 'flex', alignItems: 'center', gap: 4 }}>
            <ChevronLeft size={12}/> Back to Listing
          </button>
          <div>
            <h1 style={{ fontSize: 15, fontWeight: 700, color: C.text, margin: 0,
              display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ width: 28, height: 28, borderRadius: 7,
                background: `linear-gradient(135deg, ${C.primary}, #7c3aed)`,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                boxShadow: '0 2px 6px rgba(79,70,229,0.3)' }}>
                <FileText size={14} color="#fff"/>
              </div>
              Listing Session Logs
            </h1>
            <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4, paddingLeft: 38 }}>
              Review past /listing/generate runs · per-session log files + step timings
            </div>
          </div>
        </div>
        <button onClick={loadSessions}
          style={{ height: 32, padding: '0 14px', borderRadius: 8, fontSize: 12,
            fontWeight: 700, color: '#fff', cursor: 'pointer', border: 'none',
            background: 'linear-gradient(135deg, #4f46e5, #7c3aed)',
            display: 'flex', alignItems: 'center', gap: 6 }}>
          <RefreshCw size={13}/> Refresh
        </button>
      </div>

      {/* Filters */}
      <div style={{
        display: 'flex', gap: 8, alignItems: 'center',
        background: '#fff', border: `1px solid ${C.cardBorder}`,
        borderRadius: 8, padding: '8px 12px',
      }}>
        <Filter size={12} color={C.primary}/>
        <span style={{ fontSize: 10, fontWeight: 700, color: C.textMuted,
                       textTransform: 'uppercase' }}>Status</span>
        <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}
          style={{ height: 26, fontSize: 11, fontWeight: 600,
            border: `1px solid ${C.cardBorder}`, borderRadius: 6, padding: '0 6px',
            background: '#fff', color: C.text, cursor: 'pointer' }}>
          <option value="">All</option>
          <option value="RUNNING">Running</option>
          <option value="SUCCESS">Success</option>
          <option value="FAILED">Failed</option>
        </select>
        <span style={{ fontSize: 10, fontWeight: 700, color: C.textMuted,
                       textTransform: 'uppercase' }}>Mode</span>
        <select value={filterMode} onChange={(e) => setFilterMode(e.target.value)}
          style={{ height: 26, fontSize: 11, fontWeight: 600,
            border: `1px solid ${C.cardBorder}`, borderRadius: 6, padding: '0 6px',
            background: '#fff', color: C.text, cursor: 'pointer' }}>
          <option value="">All</option>
          <option value="sequential">Sequential</option>
          <option value="python_parallel">Python Parallel</option>
          <option value="sql_parallel">SQL Parallel</option>
        </select>
        <div style={{ flex: 1 }}/>
        <Search size={12} color={C.textMuted}/>
        <input value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Search session id / user / error..."
          style={{ height: 26, fontSize: 11, padding: '0 8px',
            border: `1px solid ${C.cardBorder}`, borderRadius: 6,
            background: '#fff', minWidth: 280 }}/>
        <span style={{ fontSize: 10, color: C.textMuted }}>
          {filtered.length} / {sessions.length}
        </span>
      </div>

      {/* Two columns: sessions table + log viewer */}
      <div style={{ display: 'grid', gridTemplateColumns: '480px 1fr', gap: 10 }}>

        {/* Sessions table */}
        <div style={{
          background: '#fff', border: `1px solid ${C.cardBorder}`,
          borderRadius: 8, overflow: 'hidden', maxHeight: '70vh',
          display: 'flex', flexDirection: 'column',
        }}>
          <div style={{ padding: '6px 10px', background: '#f8fafc',
            borderBottom: `1px solid ${C.cardBorder}`, fontSize: 11,
            fontWeight: 700, color: C.text, display: 'flex',
            alignItems: 'center', gap: 6 }}>
            <Activity size={12} color={C.primary}/> Recent Sessions
          </div>
          <div style={{ overflowY: 'auto', flex: 1 }}>
            {loading && (
              <div style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 11 }}>
                Loading...
              </div>
            )}
            {!loading && filtered.length === 0 && (
              <div style={{ padding: 20, textAlign: 'center', color: C.textMuted, fontSize: 11 }}>
                No sessions match the current filter.
              </div>
            )}
            {filtered.map((s) => (
              <div key={s.session_id}
                onClick={() => loadSelected(s.session_id)}
                style={{
                  padding: '8px 10px',
                  borderBottom: '1px solid #f1f5f9',
                  cursor: 'pointer',
                  background: selectedId === s.session_id ? '#eef2ff' : '#fff',
                }}>
                <div style={{ display: 'flex', alignItems: 'center',
                  justifyContent: 'space-between', gap: 6 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: C.text,
                    fontFamily: 'monospace' }}>
                    {s.session_id}
                  </div>
                  <span style={{
                    fontSize: 9, fontWeight: 700, padding: '1px 6px',
                    borderRadius: 3, color: '#fff',
                    background: STATUS_COLOR[s.status] || '#6b7280',
                  }}>{s.status}</span>
                </div>
                <div style={{ fontSize: 9, color: C.textMuted, marginTop: 2,
                  display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <span>{fmt(s.started_at)}</span>
                  <span>· {s.user || 'system'}</span>
                  <span>· {s.allocation_mode || 'n/a'}</span>
                  {s.workers ? <span>· {s.workers}w</span> : null}
                </div>
                <div style={{ fontSize: 10, color: C.textSub, marginTop: 3,
                  display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                  <span>⏱ {fmtSec(s.duration_sec)}</span>
                  {s.alloc_rows != null && (
                    <span>rows: <strong>{(s.alloc_rows || 0).toLocaleString()}</strong></span>
                  )}
                  {s.failed_majcats > 0 && (
                    <span style={{ color: '#dc2626' }}>
                      ✗ {s.failed_majcats} failed
                    </span>
                  )}
                </div>
                {s.error_msg && (
                  <div style={{ fontSize: 9, color: '#7f1d1d', marginTop: 3,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {s.error_msg}
                  </div>
                )}
                {/* Row actions: Kill (if RUNNING) or Delete (if finished) */}
                <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
                  {s.status === 'RUNNING' ? (
                    <button onClick={(e) => handleKill(s.session_id, e)}
                      disabled={busyId === s.session_id}
                      title="Force-terminate this RUNNING session"
                      style={{
                        height: 22, padding: '0 8px', borderRadius: 4,
                        fontSize: 9, fontWeight: 700, color: '#fff',
                        border: 'none',
                        cursor: busyId === s.session_id ? 'not-allowed' : 'pointer',
                        background: busyId === s.session_id
                          ? '#94a3b8'
                          : 'linear-gradient(135deg, #dc2626, #b91c1c)',
                        display: 'inline-flex', alignItems: 'center', gap: 3,
                      }}>
                      <Square size={9}/> Kill
                    </button>
                  ) : (
                    <button onClick={(e) => handleDelete(s.session_id, e)}
                      disabled={busyId === s.session_id}
                      title="Permanently delete this session and its log file"
                      style={{
                        height: 22, padding: '0 8px', borderRadius: 4,
                        fontSize: 9, fontWeight: 700,
                        color: busyId === s.session_id ? '#94a3b8' : '#b91c1c',
                        background: '#fff',
                        border: `1px solid ${busyId === s.session_id ? '#e2e8f0' : '#fecaca'}`,
                        cursor: busyId === s.session_id ? 'not-allowed' : 'pointer',
                        display: 'inline-flex', alignItems: 'center', gap: 3,
                      }}>
                      <Trash2 size={9}/> Delete
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Log viewer */}
        <div style={{
          background: '#fff', border: `1px solid ${C.cardBorder}`,
          borderRadius: 8, overflow: 'hidden', maxHeight: '70vh',
          display: 'flex', flexDirection: 'column',
        }}>
          <div style={{ padding: '6px 10px', background: '#f8fafc',
            borderBottom: `1px solid ${C.cardBorder}`, fontSize: 11,
            fontWeight: 700, color: C.text, display: 'flex',
            alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <FileText size={12} color={C.primary}/>
            {selectedId ? (
              <span style={{ fontFamily: 'monospace' }}>{selectedId}</span>
            ) : (
              <span style={{ color: C.textMuted, fontWeight: 500 }}>
                Select a session on the left to view its log
              </span>
            )}
            <div style={{ flex: 1 }}/>
            {selectedMeta && (
              <>
                <span style={{ fontSize: 9, color: C.textMuted }}>
                  duration <strong style={{ color: C.text }}>{fmtSec(selectedMeta.duration_sec)}</strong>
                </span>
                <span style={{ fontSize: 9, color: C.textMuted }}>
                  rows <strong style={{ color: C.text }}>{(selectedMeta.alloc_rows || 0).toLocaleString()}</strong>
                </span>
                <span style={{ fontSize: 9, color: C.textMuted }}>
                  ship <strong style={{ color: C.text }}>{Math.round(selectedMeta.ship_qty_total || 0).toLocaleString()}</strong>
                </span>
                <span style={{ fontSize: 9, color: C.textMuted }}>
                  hold <strong style={{ color: C.text }}>{Math.round(selectedMeta.hold_qty_total || 0).toLocaleString()}</strong>
                </span>
              </>
            )}
            {selectedId && (
              <label style={{ fontSize: 10, color: C.textMuted, cursor: 'pointer',
                display: 'flex', alignItems: 'center', gap: 3 }}>
                <input type="checkbox" checked={tailMode}
                  onChange={(e) => setTailMode(e.target.checked)}/>
                tail (500 lines)
              </label>
            )}
          </div>
          <pre style={{
            margin: 0, padding: '8px 12px', flex: 1, overflow: 'auto',
            fontFamily: 'Consolas, Monaco, monospace', fontSize: 10,
            lineHeight: 1.45, color: '#1e293b', background: '#fafafa',
            whiteSpace: 'pre', tabSize: 4,
          }}>
            {logLoading ? 'Loading...' :
             logText || (selectedId
                ? '(no log content)'
                : 'No session selected.')}
          </pre>
        </div>
      </div>
    </div>
  )
}
