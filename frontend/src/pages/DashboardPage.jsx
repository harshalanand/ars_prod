import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Table2, Upload, PackageCheck, Users, ArrowRight } from 'lucide-react'
import { tablesAPI, allocAPI } from '@/services/api'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import useAuthStore from '@/store/authStore'

export default function DashboardPage() {
  const { user } = useAuthStore()
  const [tables, setTables] = useState([])
  const [allocs, setAllocs] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const load = async () => {
      try {
        const [t, a] = await Promise.allSettled([
          tablesAPI.list(),
          allocAPI.list({ page_size: 5 }),
        ])
        if (t.status === 'fulfilled') setTables(t.value.data.data || [])
        if (a.status === 'fulfilled') setAllocs(a.value.data.data?.allocations || [])
      } finally { setLoading(false) }
    }
    load()
  }, [])

  const stats = [
    { label: 'Tables', value: tables.length, icon: Table2, color: 'bg-blue-500', to: '/tables' },
    { label: 'Allocations', value: allocs.length, icon: PackageCheck, color: 'bg-emerald-500', to: '/allocations' },
    { label: 'Total Rows', value: tables.reduce((s, t) => s + (t.row_count || 0), 0).toLocaleString(), icon: Upload, color: 'bg-purple-500', to: '/tables' },
  ]

  const chartData = tables.slice(0, 8).map(t => ({ name: (t.display_name || t.table_name || '').slice(0, 15), rows: t.row_count || 0 }))

  return (
    <div className="space-y-4">
      <div>
        <h1 className="page-title">Welcome back{user?.full_name ? `, ${user.full_name}` : ''}</h1>
        <p className="page-subtitle">Here's your system overview</p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {stats.map(s => (
          <Link key={s.label} to={s.to} className="card p-4 hover:shadow-md transition-shadow group">
            <div className="flex items-center justify-between">
              <div>
                <div className="stat-label">{s.label}</div>
                <div className="stat-value mt-0.5">{loading ? '...' : s.value}</div>
              </div>
              <div className={`w-10 h-10 rounded-xl ${s.color} flex items-center justify-center shadow-lg`}>
                <s.icon size={18} className="text-white" />
              </div>
            </div>
          </Link>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Chart */}
        <div className="card">
          <div className="card-header flex items-center justify-between">
            <h3 className="font-semibold text-[13px] text-gray-900">Table Row Counts</h3>
            <Link to="/tables" className="text-[11px] text-primary-600 hover:text-primary-700 flex items-center gap-1">
              View all <ArrowRight size={12} />
            </Link>
          </div>
          <div className="card-body h-56">
            {chartData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData} margin={{ top: 5, right: 10, left: -10, bottom: 5 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                  <XAxis dataKey="name" tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip />
                  <Bar dataKey="rows" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex items-center justify-center h-full text-gray-400">No table data yet</div>
            )}
          </div>
        </div>

        {/* Recent Allocations */}
        <div className="card">
          <div className="card-header flex items-center justify-between">
            <h3 className="font-semibold text-[13px] text-gray-900">Recent Allocations</h3>
            <Link to="/allocations" className="text-[11px] text-primary-600 hover:text-primary-700 flex items-center gap-1">
              View all <ArrowRight size={12} />
            </Link>
          </div>
          <div className="card-body">
            {allocs.length > 0 ? (
              <div className="space-y-2">
                {allocs.map(a => (
                  <Link key={a.id} to={`/allocations/${a.id}`} className="flex items-center justify-between p-2.5 rounded-lg hover:bg-gray-50 border border-gray-100 transition-colors">
                    <div>
                      <div className="text-[12px] font-medium text-gray-900">{a.allocation_name || a.allocation_code}</div>
                      <div className="text-[10px] text-gray-500">{a.allocation_type} • {a.total_qty?.toLocaleString() || 0} units</div>
                    </div>
                    <span className={`badge ${a.status === 'EXECUTED' ? 'badge-success' : a.status === 'APPROVED' ? 'badge-primary' : a.status === 'CANCELLED' ? 'badge-danger' : 'badge-warning'}`}>
                      {a.status}
                    </span>
                  </Link>
                ))}
              </div>
            ) : (
              <div className="text-center py-6 text-gray-400 text-[11px]">No allocations yet</div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
