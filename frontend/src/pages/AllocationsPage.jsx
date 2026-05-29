import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Plus, Search, PackageCheck, Filter } from 'lucide-react'
import { allocAPI } from '@/services/api'
import { format } from 'date-fns'
import useAuthStore from '@/store/authStore'

const STATUS_COLORS = {
  DRAFT: 'badge-warning', IN_PROGRESS: 'badge-primary', APPROVED: 'badge-success',
  EXECUTED: 'badge-gray', CANCELLED: 'badge-danger',
}

export default function AllocationsPage() {
  const [allocs, setAllocs] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const { hasPermission } = useAuthStore()

  const load = async () => {
    setLoading(true)
    try {
      const params = { page, page_size: 20 }
      if (statusFilter) params.status = statusFilter
      const { data } = await allocAPI.list(params)
      setAllocs(data.data?.allocations || [])
      setTotal(data.data?.total || 0)
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [page, statusFilter])

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Allocations</h1>
          <p className="text-gray-500 text-sm mt-0.5">Manage product allocation runs</p>
        </div>
        {hasPermission('ALLOC_CREATE') && (
          <Link to="/allocations/new" className="btn-primary"><Plus size={16} /> New Allocation</Link>
        )}
      </div>

      <div className="flex gap-3">
        <select value={statusFilter} onChange={e => { setStatusFilter(e.target.value); setPage(1) }} className="input w-44">
          <option value="">All Statuses</option>
          {['DRAFT', 'IN_PROGRESS', 'APPROVED', 'EXECUTED', 'CANCELLED'].map(s => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 border-b text-left">
              <th className="px-4 py-3 font-semibold text-gray-600">Code</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Name</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Type</th>
              <th className="px-4 py-3 font-semibold text-gray-600 text-right">Qty</th>
              <th className="px-4 py-3 font-semibold text-gray-600 text-right">Stores</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Status</th>
              <th className="px-4 py-3 font-semibold text-gray-600">Created</th>
              <th className="px-4 py-3 font-semibold text-gray-600">By</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan="8" className="px-4 py-10 text-center text-gray-400">Loading...</td></tr>
            ) : allocs.length === 0 ? (
              <tr><td colSpan="8" className="px-4 py-10 text-center text-gray-400">No allocations found</td></tr>
            ) : (
              allocs.map(a => (
                <tr key={a.id} className="border-b hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <Link to={`/allocations/${a.id}`} className="text-primary-600 font-medium hover:underline">{a.allocation_code}</Link>
                  </td>
                  <td className="px-4 py-3 text-gray-900">{a.allocation_name}</td>
                  <td className="px-4 py-3"><span className="badge-gray">{a.allocation_type}</span></td>
                  <td className="px-4 py-3 text-right font-medium">{(a.total_qty || 0).toLocaleString()}</td>
                  <td className="px-4 py-3 text-right">{a.total_stores || 0}</td>
                  <td className="px-4 py-3"><span className={STATUS_COLORS[a.status] || 'badge-gray'}>{a.status}</span></td>
                  <td className="px-4 py-3 text-gray-500">{a.created_at ? format(new Date(a.created_at), 'dd MMM yy HH:mm') : '-'}</td>
                  <td className="px-4 py-3 text-gray-500">{a.created_by}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {total > 20 && (
        <div className="flex justify-center gap-2">
          <button disabled={page <= 1} onClick={() => setPage(p => p - 1)} className="btn-secondary btn-sm">Previous</button>
          <span className="text-sm text-gray-500 px-3 py-1.5">Page {page}</span>
          <button disabled={allocs.length < 20} onClick={() => setPage(p => p + 1)} className="btn-secondary btn-sm">Next</button>
        </div>
      )}
    </div>
  )
}
