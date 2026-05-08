import { useEffect, useState, useMemo } from 'react'
import { useParams, Link } from 'react-router-dom'
import { ArrowLeft, Check, X, Play, RefreshCw, Download } from 'lucide-react'
import { allocAPI } from '@/services/api'
import { AgGridReact } from 'ag-grid-react'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-alpine.css'
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer, BarChart, Bar, XAxis, YAxis, CartesianGrid } from 'recharts'
import toast from 'react-hot-toast'
import useAuthStore from '@/store/authStore'

const STATUS_COLORS = {
  DRAFT: 'badge-warning', IN_PROGRESS: 'badge-primary', APPROVED: 'badge-success',
  EXECUTED: 'badge-gray', CANCELLED: 'badge-danger',
}
const PIE_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16']

export default function AllocationDetailPage() {
  const { id } = useParams()
  const [summary, setSummary] = useState(null)
  const [details, setDetails] = useState([])
  const [detailTotal, setDetailTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [actioning, setActioning] = useState(false)
  const { hasPermission } = useAuthStore()

  const loadSummary = async () => {
    try {
      const { data } = await allocAPI.summary(id)
      setSummary(data.data)
    } catch {}
  }

  const loadDetails = async (p = 1) => {
    try {
      const { data } = await allocAPI.details(id, { page: p, page_size: 100 })
      setDetails(data.data?.details || [])
      setDetailTotal(data.data?.total || 0)
    } catch {}
  }

  useEffect(() => { setLoading(true); Promise.all([loadSummary(), loadDetails()]).finally(() => setLoading(false)) }, [id])
  useEffect(() => { loadDetails(page) }, [page])

  const handleAction = async (action) => {
    setActioning(true)
    try {
      if (action === 'approve') await allocAPI.approve(id)
      else if (action === 'execute') await allocAPI.execute(id)
      else if (action === 'cancel') await allocAPI.cancel(id)
      toast.success(`Allocation ${action}d`)
      loadSummary()
    } catch {} finally { setActioning(false) }
  }

  const colDefs = useMemo(() => [
    { field: 'store_code', headerName: 'Store', pinned: 'left', width: 120 },
    { field: 'store_name', headerName: 'Store Name', width: 160 },
    { field: 'store_grade', headerName: 'Grade', width: 80 },
    { field: 'variant_code', headerName: 'Variant', width: 130 },
    { field: 'size', headerName: 'Size', width: 80 },
    { field: 'color', headerName: 'Color', width: 100 },
    { field: 'allocated_qty', headerName: 'Allocated', width: 100, type: 'numericColumn', cellStyle: { fontWeight: 600 } },
    { field: 'override_qty', headerName: 'Override', width: 100, type: 'numericColumn' },
    { field: 'final_qty', headerName: 'Final', width: 100, type: 'numericColumn', cellStyle: { fontWeight: 700, color: '#2563eb' } },
  ], [])

  const defaultColDef = useMemo(() => ({ sortable: true, filter: true, resizable: true, floatingFilter: true }), [])

  const header = summary?.header || {}
  const gradeData = summary?.by_grade ? Object.entries(summary.by_grade).map(([g, q]) => ({ name: `Grade ${g}`, value: q })) : []
  const sizeData = summary?.by_size ? Object.entries(summary.by_size).map(([s, q]) => ({ name: s, qty: q })) : []

  const exportCSV = () => {
    if (!details.length) return
    const cols = Object.keys(details[0])
    const csv = [cols.join(','), ...details.map(r => cols.map(c => `"${r[c] ?? ''}"`).join(','))].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `allocation_${id}.csv`; a.click()
  }

  if (loading) return <div className="py-20 text-center text-gray-400">Loading allocation...</div>

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-3">
          <Link to="/allocations" className="p-2 hover:bg-gray-100 rounded-lg"><ArrowLeft size={18} /></Link>
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-xl font-bold text-gray-900">{header.allocation_name || header.allocation_code || `Allocation #${id}`}</h1>
              <span className={STATUS_COLORS[header.status] || 'badge-gray'}>{header.status}</span>
            </div>
            <div className="flex items-center gap-4 text-sm text-gray-500 mt-1">
              <span>{header.allocation_code}</span>
              <span>{header.allocation_type} • {header.allocation_basis}</span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={exportCSV} className="btn-secondary btn-sm"><Download size={14} /> CSV</button>
          {header.status === 'DRAFT' && hasPermission('ALLOC_APPROVE') && (
            <button onClick={() => handleAction('approve')} disabled={actioning} className="btn-success btn-sm"><Check size={14} /> Approve</button>
          )}
          {header.status === 'APPROVED' && hasPermission('ALLOC_EXECUTE') && (
            <button onClick={() => handleAction('execute')} disabled={actioning} className="btn-primary btn-sm"><Play size={14} /> Execute</button>
          )}
          {!['EXECUTED', 'CANCELLED'].includes(header.status) && hasPermission('ALLOC_UPDATE') && (
            <button onClick={() => handleAction('cancel')} disabled={actioning} className="btn-danger btn-sm"><X size={14} /> Cancel</button>
          )}
        </div>
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-5 gap-4">
        {[
          { l: 'Total Qty', v: (header.total_qty || 0).toLocaleString() },
          { l: 'Total Stores', v: header.total_stores || 0 },
          { l: 'Variants', v: header.total_variants || '-' },
          { l: 'Category', v: header.category || '-' },
          { l: 'Warehouse', v: header.warehouse_code || '-' },
        ].map(s => (
          <div key={s.l} className="stat-card">
            <div className="stat-label">{s.l}</div>
            <div className="stat-value text-lg">{s.v}</div>
          </div>
        ))}
      </div>

      {/* Charts */}
      {(gradeData.length > 0 || sizeData.length > 0) && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          {gradeData.length > 0 && (
            <div className="card p-5">
              <h3 className="font-semibold text-gray-900 mb-3">By Store Grade</h3>
              <div className="h-48">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie data={gradeData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={70} label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}>
                      {gradeData.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                    </Pie>
                    <Tooltip />
                  </PieChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}
          {sizeData.length > 0 && (
            <div className="card p-5">
              <h3 className="font-semibold text-gray-900 mb-3">By Size</h3>
              <div className="h-48">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={sizeData} margin={{ top: 5, right: 10, left: -10, bottom: 5 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
                    <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Bar dataKey="qty" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Detail Grid */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h3 className="font-semibold text-gray-900">Allocation Details</h3>
          <span className="text-sm text-gray-500">{detailTotal.toLocaleString()} rows</span>
        </div>
        <div className="ag-theme-alpine" style={{ width: '100%', height: 400 }}>
          <AgGridReact
            rowData={details}
            columnDefs={colDefs}
            defaultColDef={defaultColDef}
            animateRows
            pagination={false}
          />
        </div>
        {detailTotal > 100 && (
          <div className="px-4 py-3 border-t flex items-center justify-between">
            <span className="text-sm text-gray-500">Page {page}</span>
            <div className="flex gap-2">
              <button disabled={page <= 1} onClick={() => setPage(p => p - 1)} className="btn-secondary btn-sm">Previous</button>
              <button disabled={details.length < 100} onClick={() => setPage(p => p + 1)} className="btn-secondary btn-sm">Next</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
