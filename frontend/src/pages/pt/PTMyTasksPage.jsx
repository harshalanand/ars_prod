// Project Tracker — My Tasks (filtered to current user's open assignments)
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ptAPI } from '@/services/api'
import { StatusBadge, PriorityChip, PhaseChip } from '@/components/pt/StatusBadge'

export default function PTMyTasksPage() {
  const nav = useNavigate()
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    ptAPI.myTasks()
      .then(res => setRows(res.data?.data || []))
      .finally(() => setLoading(false))
  }, [])

  return (
    <div style={{ padding: 20, background: '#f8fafc', minHeight: '100%' }}>
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontSize: 20, fontWeight: 700, color: '#111827' }}>My Tasks</div>
        <div style={{ fontSize: 12, color: '#6b7280' }}>
          Open projects where you are the owner or an assignee, sorted by priority then due date.
        </div>
      </div>

      <div style={{ background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', overflow: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #e5e7eb', textAlign: 'left', color: '#6b7280', background: '#f9fafb' }}>
              <th style={{ padding: '10px 12px' }}>Code</th>
              <th style={{ padding: '10px 12px' }}>Name</th>
              <th style={{ padding: '10px 12px' }}>Status</th>
              <th style={{ padding: '10px 12px' }}>Priority</th>
              <th style={{ padding: '10px 12px' }}>Phase</th>
              <th style={{ padding: '10px 12px' }}>Due</th>
              <th style={{ padding: '10px 12px', textAlign: 'right' }}>%</th>
            </tr>
          </thead>
          <tbody>
            {loading && <tr><td colSpan={7} style={{ padding: 20, textAlign: 'center', color: '#6b7280' }}>Loading…</td></tr>}
            {!loading && rows.length === 0 && (
              <tr><td colSpan={7} style={{ padding: 30, textAlign: 'center', color: '#6b7280' }}>
                Nothing assigned to you. 🎉
              </td></tr>
            )}
            {!loading && rows.map(r => (
              <tr key={r.PROJECT_ID}
                  onClick={() => nav(`/pt/projects/${r.PROJECT_ID}`)}
                  style={{ borderBottom: '1px solid #f1f5f9', cursor: 'pointer' }}>
                <td style={{ padding: '8px 12px', fontFamily: 'monospace', color: '#4f46e5', fontWeight: 600 }}>
                  {r.PROJECT_CODE}
                </td>
                <td style={{ padding: '8px 12px' }}>{r.NAME}</td>
                <td style={{ padding: '8px 12px' }}><StatusBadge value={r.STATUS} /></td>
                <td style={{ padding: '8px 12px' }}><PriorityChip value={r.PRIORITY} /></td>
                <td style={{ padding: '8px 12px' }}><PhaseChip value={r.PHASE} /></td>
                <td style={{ padding: '8px 12px', color: r.IS_OVERDUE ? '#dc2626' : '#374151', fontWeight: r.IS_OVERDUE ? 700 : 400 }}>
                  {r.DUE_DATE?.slice(0,10) || '—'}
                </td>
                <td style={{ padding: '8px 12px', textAlign: 'right' }}>{r.PROGRESS_PCT}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
