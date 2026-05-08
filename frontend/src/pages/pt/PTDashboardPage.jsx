// Project Tracker — Dashboard
// Top: 5 KPI tiles. Mid: 3 distribution donut/bar charts. Bottom: top-10 overdue.
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Activity, AlertTriangle, CheckCircle2, ListChecks, ShieldAlert, User,
} from 'lucide-react'
import {
  PieChart, Pie, Cell, BarChart, Bar, XAxis, YAxis,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts'
import { ptAPI } from '@/services/api'
import { StatusBadge, PriorityChip, PhaseChip } from '@/components/pt/StatusBadge'

const COLORS = {
  CRITICAL: '#DC2626', HIGH: '#EA580C', MEDIUM: '#CA8A04', LOW: '#16A34A',
  IN_PROGRESS: '#2563EB', NOT_STARTED: '#6B7280', BLOCKED: '#DC2626',
  ON_HOLD: '#9CA3AF', COMPLETED: '#16A34A', CANCELLED: '#94A3B8', DRAFT: '#A3A3A3',
  PHASE_1: '#6D28D9', PHASE_2: '#0369A1', PHASE_3: '#15803D',
  BACKLOG: '#4B5563', ICEBOX: '#94A3B8', '(none)': '#D1D5DB',
}

function KpiTile({ icon: Icon, label, value, color = '#4f46e5', onClick }) {
  return (
    <div onClick={onClick} style={{
      background: '#fff', borderRadius: 10, padding: '14px 16px',
      boxShadow: '0 1px 2px rgba(0,0,0,.05)',
      border: '1px solid #e5e7eb',
      cursor: onClick ? 'pointer' : 'default',
      transition: 'transform .12s, box-shadow .12s',
    }} onMouseEnter={e => { if (onClick) e.currentTarget.style.transform = 'translateY(-1px)' }}
       onMouseLeave={e => e.currentTarget.style.transform = 'translateY(0)'}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: '#6b7280', textTransform: 'uppercase', letterSpacing: 0.5 }}>
          {label}
        </span>
        <Icon size={18} color={color} />
      </div>
      <div style={{ fontSize: 26, fontWeight: 700, color: '#111827' }}>{value ?? '—'}</div>
    </div>
  )
}

function ChartCard({ title, children }) {
  return (
    <div style={{
      background: '#fff', borderRadius: 10, padding: '14px 16px',
      boxShadow: '0 1px 2px rgba(0,0,0,.05)', border: '1px solid #e5e7eb',
    }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: '#111827', marginBottom: 10 }}>{title}</div>
      {children}
    </div>
  )
}

export default function PTDashboardPage() {
  const nav = useNavigate()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    ptAPI.dashboard()
      .then(res => setData(res.data?.data))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div style={{ padding: 24, color: '#6b7280' }}>Loading…</div>
  if (!data) return <div style={{ padding: 24, color: '#6b7280' }}>No data.</div>

  const { kpi, status_distribution, priority_distribution, phase_distribution, top_overdue } = data
  return (
    <div style={{ padding: 20, background: '#f8fafc', minHeight: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700, color: '#111827' }}>Project Tracker</div>
          <div style={{ fontSize: 12, color: '#6b7280' }}>Snapshot of all active projects, sub-projects and tasks.</div>
        </div>
        <button onClick={() => nav('/pt/projects')} style={{
          padding: '8px 14px', background: '#4f46e5', color: '#fff', border: 'none',
          borderRadius: 6, cursor: 'pointer', fontSize: 12, fontWeight: 600,
        }}>All projects →</button>
      </div>

      {/* KPI tiles */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12, marginBottom: 16 }}>
        <KpiTile icon={ListChecks}  label="Open"          value={kpi.open}         color="#2563eb" onClick={() => nav('/pt/projects')} />
        <KpiTile icon={ShieldAlert} label="Critical Open" value={kpi.critical_open} color="#dc2626" onClick={() => nav('/pt/projects?priority=CRITICAL')} />
        <KpiTile icon={AlertTriangle} label="Overdue"     value={kpi.overdue}      color="#ea580c" onClick={() => nav('/pt/projects?overdue=1')} />
        <KpiTile icon={User}        label="My Open"       value={kpi.my_open}      color="#7c3aed" onClick={() => nav('/pt/my-tasks')} />
        <KpiTile icon={CheckCircle2} label="Completed (7d)" value={kpi.completed_7d} color="#16a34a" />
      </div>

      {/* Charts */}
      <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1fr 1fr', gap: 12, marginBottom: 16 }}>
        <ChartCard title="Status distribution">
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie data={status_distribution} dataKey="count" nameKey="label" innerRadius={45} outerRadius={75} paddingAngle={2}>
                {status_distribution.map((d, i) => (
                  <Cell key={i} fill={COLORS[d.label] || '#94A3B8'} />
                ))}
              </Pie>
              <Tooltip /><Legend wrapperStyle={{ fontSize: 11 }} />
            </PieChart>
          </ResponsiveContainer>
        </ChartCard>
        <ChartCard title="Open by priority">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={priority_distribution}>
              <XAxis dataKey="label" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
              <Tooltip />
              <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                {priority_distribution.map((d, i) => (
                  <Cell key={i} fill={COLORS[d.label] || '#94A3B8'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
        <ChartCard title="Open by phase">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={phase_distribution}>
              <XAxis dataKey="label" tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
              <Tooltip />
              <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                {phase_distribution.map((d, i) => (
                  <Cell key={i} fill={COLORS[d.label] || '#94A3B8'} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Top overdue */}
      <div style={{
        background: '#fff', borderRadius: 10, padding: '14px 16px',
        boxShadow: '0 1px 2px rgba(0,0,0,.05)', border: '1px solid #e5e7eb',
      }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: '#111827', marginBottom: 10 }}>
          Top 10 overdue
        </div>
        {top_overdue.length === 0 ? (
          <div style={{ color: '#6b7280', fontSize: 12, padding: '12px 0' }}>Nothing overdue 🎉</div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #e5e7eb', textAlign: 'left', color: '#6b7280' }}>
                <th style={{ padding: '6px 8px' }}>Code</th>
                <th style={{ padding: '6px 8px' }}>Name</th>
                <th style={{ padding: '6px 8px' }}>Owner</th>
                <th style={{ padding: '6px 8px' }}>Priority</th>
                <th style={{ padding: '6px 8px' }}>Due</th>
                <th style={{ padding: '6px 8px', textAlign: 'right' }}>Days late</th>
              </tr>
            </thead>
            <tbody>
              {top_overdue.map(r => (
                <tr key={r.PROJECT_ID} onClick={() => nav(`/pt/projects/${r.PROJECT_ID}`)}
                    style={{ borderBottom: '1px solid #f1f5f9', cursor: 'pointer' }}>
                  <td style={{ padding: '8px', fontFamily: 'monospace', color: '#4f46e5' }}>{r.PROJECT_CODE}</td>
                  <td style={{ padding: '8px' }}>{r.NAME}</td>
                  <td style={{ padding: '8px', color: '#6b7280' }}>{r.OWNER_USERNAME || '—'}</td>
                  <td style={{ padding: '8px' }}><PriorityChip value={r.PRIORITY} /></td>
                  <td style={{ padding: '8px' }}>{r.DUE_DATE?.slice(0,10)}</td>
                  <td style={{ padding: '8px', textAlign: 'right', color: '#dc2626', fontWeight: 700 }}>
                    {r.days_overdue}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
