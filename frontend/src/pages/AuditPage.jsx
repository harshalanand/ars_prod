import { useState, useMemo, useEffect, useCallback } from 'react'
import { Search, Download, RefreshCw, X, Eye, FileText } from 'lucide-react'
import { auditAPI, tablesAPI, usersAPI } from '@/services/api'
import api from '@/services/api'
import { AgGridReact } from 'ag-grid-react'
import 'ag-grid-community/styles/ag-grid.css'
import 'ag-grid-community/styles/ag-theme-alpine.css'
import { format, subDays } from 'date-fns'
import toast from 'react-hot-toast'

export default function AuditPage() {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [total, setTotal] = useState(0)
  const [tables, setTables] = useState([])
  const [users, setUsers] = useState([])
  const [selectedBatch, setSelectedBatch] = useState(null)
  const [batchChanges, setBatchChanges] = useState([])
  const [batchLoading, setBatchLoading] = useState(false)
  const [filters, setFilters] = useState({
    table_name: '',
    action_type: '',
    changed_by: '',
    date_from: format(subDays(new Date(), 7), 'yyyy-MM-dd'),
    date_to: format(new Date(), 'yyyy-MM-dd'),
    page_size: 500,
  })

  const update = (k, v) => setFilters(f => ({ ...f, [k]: v }))

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = {}
      if (filters.table_name) params.table_name = filters.table_name
      if (filters.action_type) params.action_type = filters.action_type
      if (filters.changed_by) params.changed_by = filters.changed_by
      if (filters.date_from) params.date_from = filters.date_from
      if (filters.date_to) params.date_to = filters.date_to
      params.page_size = filters.page_size
      const { data } = await auditAPI.list(params)
      setRows(data.data?.logs || data.data || [])
      setTotal(data.data?.total || 0)
    } catch (err) {
      console.error('Failed to load audit logs:', err)
      toast.error('Failed to load audit logs')
    } finally { setLoading(false) }
  }, [filters])

  // Load tables and users for dropdowns
  useEffect(() => {
    const loadOptions = async () => {
      try {
        const [tablesRes, usersRes] = await Promise.all([
          tablesAPI.listAll(),
          usersAPI.list({ page_size: 1000 }),
        ])
        setTables(tablesRes.data?.data || [])
        setUsers(usersRes.data?.data?.users || usersRes.data?.data || [])
      } catch (err) {
        console.error('Failed to load filter options:', err)
      }
    }
    loadOptions()
  }, [])

  // Auto-load on mount
  useEffect(() => {
    load()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const loadBatchChanges = useCallback(async (batchId) => {
    if (!batchId) return
    setSelectedBatch(batchId)
    setBatchLoading(true)
    try {
      const { data } = await api.get(`/audit/changes/batch/${batchId}`, { params: { page_size: 500 } })
      setBatchChanges(data.data?.changes || [])
    } catch (err) {
      toast.error('Failed to load batch changes')
      console.error(err)
    } finally {
      setBatchLoading(false)
    }
  }, [])

  const exportBatchChanges = () => {
    if (!batchChanges.length) return
    const cols = ['row_index', 'record_key', 'action_type', 'column_name', 'old_value', 'new_value', 'changed_at']
    const csv = [cols.join(','), ...batchChanges.map(r => cols.map(c => `"${r[c] ?? ''}"`).join(','))].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `batch_changes_${selectedBatch}.csv`
    a.click()
    toast.success('Exported batch changes')
  }

  const colDefs = useMemo(() => [
    { field: 'id', headerName: 'ID', width: 80 },
    { field: 'table_name', headerName: 'Table', width: 150 },
    { field: 'action_type', headerName: 'Action', width: 120,
      cellStyle: (p) => {
        if (p.value === 'INSERT' || p.value === 'BULK_UPLOAD') return { color: '#16a34a', fontWeight: 600 }
        if (p.value === 'UPDATE') return { color: '#2563eb', fontWeight: 600 }
        if (p.value === 'DELETE') return { color: '#dc2626', fontWeight: 600 }
        return null
      }
    },
    { field: 'record_primary_key', headerName: 'Primary Key', width: 180 },
    { field: 'changed_columns', headerName: 'Changed Columns', width: 200,
      valueFormatter: (p) => {
        if (!p.value) return ''
        try {
          const cols = JSON.parse(p.value)
          if (typeof cols === 'object' && !Array.isArray(cols)) {
            // Format: {col: count}
            return Object.entries(cols).map(([k, v]) => `${k}(${v})`).join(', ')
          }
          return Array.isArray(cols) ? cols.join(', ') : p.value
        } catch { return p.value }
      }
    },
    { field: 'row_count', headerName: 'Rows', width: 80 },
    { field: 'changed_by', headerName: 'Changed By', width: 120 },
    { field: 'changed_at', headerName: 'Timestamp', width: 160,
      valueFormatter: (p) => p.value ? format(new Date(p.value), 'yyyy-MM-dd HH:mm:ss') : ''
    },
    { field: 'batch_id', headerName: 'Batch', width: 140,
      cellRenderer: (p) => {
        if (!p.value) return null
        return (
          <button
            onClick={() => loadBatchChanges(p.value)}
            className="text-primary-600 hover:underline flex items-center gap-1"
          >
            <Eye size={12} /> {p.value.substring(0, 15)}
          </button>
        )
      }
    },
    { field: 'source', headerName: 'Source', width: 90 },
    { field: 'notes', headerName: 'Notes', width: 250 },
  ], [loadBatchChanges])

  const defaultColDef = useMemo(() => ({ sortable: true, filter: true, resizable: true, floatingFilter: true }), [])

  const exportCSV = () => {
    if (!rows.length) return
    const cols = ['id', 'table_name', 'action_type', 'record_primary_key', 'changed_columns', 'row_count', 'changed_by', 'changed_at', 'batch_id', 'source', 'notes']
    const csv = [cols.join(','), ...rows.map(r => cols.map(c => `"${r[c] ?? ''}"`).join(','))].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'audit_log.csv'; a.click()
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Audit Log</h1>
        <p className="text-gray-500 text-sm mt-0.5">Track all data changes across tables - click batch ID to view details</p>
      </div>

      {/* Filters */}
      <div className="card p-4">
        <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
          <div>
            <label className="label">Table</label>
            <select value={filters.table_name} onChange={e => update('table_name', e.target.value)} className="input">
              <option value="">All Tables</option>
              {tables.map(t => <option key={t.table_name || t} value={t.table_name || t}>{t.table_name || t}</option>)}
            </select>
          </div>
          <div>
            <label className="label">Action</label>
            <select value={filters.action_type} onChange={e => update('action_type', e.target.value)} className="input">
              <option value="">All</option>
              {['INSERT', 'UPDATE', 'DELETE', 'BULK_UPLOAD', 'CREATE_TABLE', 'ALTER_TABLE', 'DROP_TABLE'].map(o => <option key={o}>{o}</option>)}
            </select>
          </div>
          <div>
            <label className="label">Changed By</label>
            <select value={filters.changed_by} onChange={e => update('changed_by', e.target.value)} className="input">
              <option value="">All Users</option>
              {users.map(u => <option key={u.username || u.id} value={u.username}>{u.full_name || u.username}</option>)}
            </select>
          </div>
          <div>
            <label className="label">From</label>
            <input type="date" value={filters.date_from} onChange={e => update('date_from', e.target.value)} className="input" />
          </div>
          <div>
            <label className="label">To</label>
            <input type="date" value={filters.date_to} onChange={e => update('date_to', e.target.value)} className="input" />
          </div>
          <div className="flex items-end gap-2">
            <button onClick={load} className="btn-primary flex-1"><Search size={14} /> Search</button>
            <button onClick={exportCSV} className="btn-secondary" title="Export CSV"><Download size={14} /></button>
          </div>
        </div>
      </div>

      {/* Grid */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <span className="text-sm text-gray-500">
            {loading ? 'Loading...' : `${rows.length.toLocaleString()} rows loaded`}
          </span>
          <button onClick={load} className="btn-ghost btn-sm" disabled={loading}>
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
        <div className="ag-theme-alpine" style={{ width: '100%', height: 500 }}>
          <AgGridReact
            rowData={rows}
            columnDefs={colDefs}
            defaultColDef={defaultColDef}
            animateRows
            pagination
            paginationPageSize={50}
          />
        </div>
      </div>

      {/* Batch Changes Modal */}
      {selectedBatch && (
        <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
          <div className="bg-white rounded-xl shadow-2xl w-full max-w-6xl max-h-[90vh] flex flex-col">
            <div className="flex items-center justify-between p-4 border-b">
              <div>
                <h2 className="text-lg font-semibold">Batch Details: {selectedBatch}</h2>
                <p className="text-sm text-gray-500">{batchChanges.length} row/column changes tracked</p>
              </div>
              <div className="flex items-center gap-2">
                <button onClick={exportBatchChanges} className="btn-secondary btn-sm" disabled={!batchChanges.length}>
                  <Download size={14} /> Export CSV
                </button>
                <button onClick={() => setSelectedBatch(null)} className="p-2 hover:bg-gray-100 rounded-lg">
                  <X size={18} />
                </button>
              </div>
            </div>
            <div className="flex-1 overflow-auto p-4">
              {batchLoading ? (
                <div className="flex items-center justify-center h-32">
                  <RefreshCw className="animate-spin text-primary-500" size={24} />
                </div>
              ) : batchChanges.length === 0 ? (
                <div className="text-center text-gray-500 py-12">
                  <FileText size={40} className="mx-auto mb-3 opacity-50" />
                  <p className="font-medium">No detailed changes recorded</p>
                  <p className="text-sm">Row-level audit may not have been enabled for this batch</p>
                </div>
              ) : (
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 sticky top-0">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium text-gray-600">Row</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600">Record Key</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600">Action</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600">Column</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600">Old Value</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600">New Value</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600">Changed At</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {batchChanges.map((c, idx) => (
                      <tr key={c.id || idx} className="hover:bg-gray-50">
                        <td className="px-3 py-2 text-gray-500">{c.row_index ?? '-'}</td>
                        <td className="px-3 py-2 font-mono text-xs">{c.record_key}</td>
                        <td className="px-3 py-2">
                          <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                            c.action_type === 'INSERT' ? 'bg-green-100 text-green-700' :
                            c.action_type === 'UPDATE' ? 'bg-blue-100 text-blue-700' :
                            c.action_type === 'DELETE' ? 'bg-red-100 text-red-700' :
                            'bg-gray-100 text-gray-700'
                          }`}>
                            {c.action_type}
                          </span>
                        </td>
                        <td className="px-3 py-2 font-medium">{c.column_name || '-'}</td>
                        <td className="px-3 py-2 text-red-600 font-mono text-xs max-w-[200px] truncate" title={c.old_value}>
                          {c.old_value ?? <span className="text-gray-400 italic">null</span>}
                        </td>
                        <td className="px-3 py-2 text-green-600 font-mono text-xs max-w-[200px] truncate" title={c.new_value}>
                          {c.new_value ?? <span className="text-gray-400 italic">null</span>}
                        </td>
                        <td className="px-3 py-2 text-gray-500 text-xs">
                          {c.changed_at ? format(new Date(c.changed_at), 'HH:mm:ss') : '-'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
