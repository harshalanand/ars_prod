import { useState, useEffect, useCallback } from 'react'
import { Settings, Table, Columns, Trash2, Plus, Edit, RefreshCw, AlertTriangle, ChevronDown, ArrowUp, ArrowDown, Save, GripVertical } from 'lucide-react'
import { tablesAPI } from '@/services/api'
import toast from 'react-hot-toast'

export default function TableManagementPage() {
  const [tables, setTables] = useState([])
  const [selectedTable, setSelectedTable] = useState(null)
  const [columns, setColumns] = useState([])
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState('')
  const [suffixFilter, setSuffixFilter] = useState('')
  
  // Modal states
  const [showAddColumn, setShowAddColumn] = useState(false)
  const [showRenameColumn, setShowRenameColumn] = useState(false)
  const [showChangeType, setShowChangeType] = useState(false)
  const [showTruncate, setShowTruncate] = useState(false)
  const [showDrop, setShowDrop] = useState(false)
  const [selectedColumn, setSelectedColumn] = useState(null)
  const [originalOrder, setOriginalOrder] = useState([])
  const [reordering, setReordering] = useState(false)
  const [dragIdx, setDragIdx] = useState(null)

  // Truncate job progress (background job + polling)
  const [truncateJob, setTruncateJob] = useState(null)
  // truncateJob shape:
  //   { jobId, table, status, phase, percent, processed, total, method?, error? }

  const suffixes = ['', 'MST', 'TXN', 'ALC', 'STK', 'RPT', 'TMP', 'LOG']

  useEffect(() => { fetchTables() }, [])

  const fetchTables = async () => {
    setLoading(true)
    try {
      const { data } = await tablesAPI.listAll()
      setTables(data.data || [])
    } catch (err) {
      toast.error('Failed to fetch tables')
    } finally {
      setLoading(false)
    }
  }

  const fetchSchema = async (tableName) => {
    setLoading(true)
    try {
      const { data } = await tablesAPI.schema(tableName)
      const cols = data.data?.columns || []
      setColumns(cols)
      setOriginalOrder(cols.map(c => c.column_name || c.name))
      setSelectedTable(tableName)
    } catch (err) {
      toast.error('Failed to fetch schema')
    } finally {
      setLoading(false)
    }
  }

  const moveColumn = (idx, dir) => {
    const newIdx = idx + dir
    if (newIdx < 0 || newIdx >= columns.length) return
    const updated = [...columns]
    const [moved] = updated.splice(idx, 1)
    updated.splice(newIdx, 0, moved)
    setColumns(updated)
  }

  const onDragStart = (idx) => setDragIdx(idx)
  const onDragOver = (e, idx) => {
    e.preventDefault()
    if (dragIdx === null || dragIdx === idx) return
    const updated = [...columns]
    const [moved] = updated.splice(dragIdx, 1)
    updated.splice(idx, 0, moved)
    setColumns(updated)
    setDragIdx(idx)
  }
  const onDragEnd = () => setDragIdx(null)

  const orderChanged = columns.length > 0 && JSON.stringify(columns.map(c => c.column_name || c.name)) !== JSON.stringify(originalOrder)

  const saveColumnOrder = async () => {
    if (!selectedTable || !orderChanged) return
    setReordering(true)
    try {
      const newOrder = columns.map(c => c.column_name || c.name)
      await tablesAPI.reorderColumns(selectedTable, newOrder)
      setOriginalOrder(newOrder)
      toast.success('Column order saved')
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to reorder columns')
    } finally { setReordering(false) }
  }

  const filteredTables = tables.filter(t => {
    const name = t.table_name || t.name || t
    const matchesName = name.toLowerCase().includes(filter.toLowerCase())
    const matchesSuffix = !suffixFilter || name.toUpperCase().includes(`_${suffixFilter}`) || name.toUpperCase().startsWith(suffixFilter)
    return matchesName && matchesSuffix
  })

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-primary-100 rounded-lg"><Settings size={24} className="text-primary-600" /></div>
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Table Management</h1>
            <p className="text-gray-500 text-sm">Manage database table schemas</p>
          </div>
        </div>
        <button onClick={fetchTables} className="btn-secondary flex items-center gap-2">
          <RefreshCw size={16} /> Refresh
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Tables List */}
        <div className="lg:col-span-1 card">
          <div className="p-4 border-b space-y-3">
            <h3 className="font-semibold">Tables</h3>
            <input
              type="text"
              placeholder="Search tables..."
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              className="input text-sm"
            />
            <select
              value={suffixFilter}
              onChange={(e) => setSuffixFilter(e.target.value)}
              className="input text-sm"
            >
              <option value="">All Modules</option>
              {suffixes.map(s => s && <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="max-h-[500px] overflow-y-auto">
            {loading && !selectedTable ? (
              <div className="p-4 text-center text-gray-500">Loading...</div>
            ) : filteredTables.length === 0 ? (
              <div className="p-4 text-center text-gray-500">No tables found</div>
            ) : (
              filteredTables.map((t, i) => {
                const name = t.table_name || t.name || t
                return (
                  <button
                    key={i}
                    onClick={() => fetchSchema(name)}
                    className={`w-full text-left px-4 py-2 text-sm hover:bg-gray-50 flex items-center gap-2 border-b ${
                      selectedTable === name ? 'bg-primary-50 text-primary-700 font-medium' : ''
                    }`}
                  >
                    <Table size={14} className="text-gray-400" />
                    <span className="truncate">{name}</span>
                  </button>
                )
              })
            )}
          </div>
        </div>

        {/* Schema Details */}
        <div className="lg:col-span-3">
          {!selectedTable ? (
            <div className="card p-12 text-center text-gray-500">
              <Columns size={48} className="mx-auto mb-4 text-gray-300" />
              <p>Select a table to view and manage its schema</p>
            </div>
          ) : (
            <div className="card">
              <div className="p-4 border-b flex items-center justify-between">
                <div>
                  <h3 className="font-semibold text-lg">{selectedTable}</h3>
                  <p className="text-sm text-gray-500">{columns.length} columns</p>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => setShowAddColumn(true)} className="btn-primary flex items-center gap-1 text-sm py-1.5">
                    <Plus size={14} /> Add Column
                  </button>
                  <button onClick={() => setShowTruncate(true)} className="btn-secondary text-orange-600 hover:bg-orange-50 flex items-center gap-1 text-sm py-1.5">
                    <RefreshCw size={14} /> Truncate
                  </button>
                  <button onClick={() => setShowDrop(true)} className="btn-secondary text-red-600 hover:bg-red-50 flex items-center gap-1 text-sm py-1.5">
                    <Trash2 size={14} /> Drop Table
                  </button>
                </div>
              </div>
              {/* Save Order button */}
              {orderChanged && (
                <div className="px-4 py-2 bg-amber-50 border-b border-amber-200 flex items-center justify-between">
                  <span className="text-xs text-amber-700 font-medium">Column order has been changed. Save to apply in SQL Server.</span>
                  <button onClick={saveColumnOrder} disabled={reordering}
                    className="btn-primary text-xs py-1 px-3 flex items-center gap-1">
                    {reordering ? <RefreshCw size={12} className="animate-spin" /> : <Save size={12} />}
                    {reordering ? 'Saving...' : 'Save Order'}
                  </button>
                </div>
              )}
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-2 py-3 text-left text-xs font-medium text-gray-500 uppercase w-8">#</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Column Name</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Data Type</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Nullable</th>
                      <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Max Length</th>
                      <th className="px-2 py-3 text-center text-xs font-medium text-gray-500 uppercase w-16">Order</th>
                      <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {columns.map((col, idx) => (
                      <tr key={idx}
                        draggable
                        onDragStart={() => onDragStart(idx)}
                        onDragOver={(e) => onDragOver(e, idx)}
                        onDragEnd={onDragEnd}
                        className={`hover:bg-gray-50 cursor-grab active:cursor-grabbing ${dragIdx === idx ? 'bg-blue-50 opacity-60' : ''}`}>
                        <td className="px-2 py-2 text-center">
                          <GripVertical size={14} className="text-gray-300 inline" />
                        </td>
                        <td className="px-4 py-2 text-sm font-medium text-gray-900">{col.column_name || col.name}</td>
                        <td className="px-4 py-2 text-sm text-gray-600">{col.data_type || col.type}</td>
                        <td className="px-4 py-2 text-sm">
                          <span className={`px-2 py-0.5 rounded text-xs ${col.is_nullable === 'YES' || col.nullable ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-700'}`}>
                            {col.is_nullable === 'YES' || col.nullable ? 'Yes' : 'No'}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-sm text-gray-600">{col.max_length || col.character_maximum_length || '-'}</td>
                        <td className="px-2 py-2 text-center">
                          <div className="flex items-center justify-center gap-0.5">
                            <button onClick={() => moveColumn(idx, -1)} disabled={idx === 0}
                              className="p-0.5 hover:bg-gray-200 rounded disabled:opacity-20" title="Move Up">
                              <ArrowUp size={12} className="text-gray-500" />
                            </button>
                            <button onClick={() => moveColumn(idx, 1)} disabled={idx === columns.length - 1}
                              className="p-0.5 hover:bg-gray-200 rounded disabled:opacity-20" title="Move Down">
                              <ArrowDown size={12} className="text-gray-500" />
                            </button>
                          </div>
                        </td>
                        <td className="px-4 py-2 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <button
                              onClick={() => { setSelectedColumn(col); setShowRenameColumn(true) }}
                              className="p-1.5 hover:bg-gray-100 rounded text-blue-600"
                              title="Rename"
                            >
                              <Edit size={14} />
                            </button>
                            <button
                              onClick={() => { setSelectedColumn(col); setShowChangeType(true) }}
                              className="p-1.5 hover:bg-gray-100 rounded text-purple-600"
                              title="Change Type"
                            >
                              <Columns size={14} />
                            </button>
                            <button
                              onClick={() => handleDeleteColumn(col)}
                              className="p-1.5 hover:bg-red-50 rounded text-red-600"
                              title="Delete"
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Add Column Modal */}
      {showAddColumn && (
        <AddColumnModal
          table={selectedTable}
          onClose={() => setShowAddColumn(false)}
          onSuccess={() => { setShowAddColumn(false); fetchSchema(selectedTable) }}
        />
      )}

      {/* Rename Column Modal */}
      {showRenameColumn && selectedColumn && (
        <RenameColumnModal
          table={selectedTable}
          column={selectedColumn}
          onClose={() => { setShowRenameColumn(false); setSelectedColumn(null) }}
          onSuccess={() => { setShowRenameColumn(false); setSelectedColumn(null); fetchSchema(selectedTable) }}
        />
      )}

      {/* Change Type Modal */}
      {showChangeType && selectedColumn && (
        <ChangeTypeModal
          table={selectedTable}
          column={selectedColumn}
          onClose={() => { setShowChangeType(false); setSelectedColumn(null) }}
          onSuccess={() => { setShowChangeType(false); setSelectedColumn(null); fetchSchema(selectedTable) }}
        />
      )}

      {/* Truncate Confirm Modal */}
      {showTruncate && (
        <ConfirmModal
          title="Truncate Table"
          message={`Are you sure you want to truncate "${selectedTable}"? All data will be permanently deleted.`}
          confirmText="Truncate"
          confirmClass="bg-orange-600 hover:bg-orange-700"
          onClose={() => setShowTruncate(false)}
          onConfirm={async () => {
            try {
              const { data } = await tablesAPI.truncate(selectedTable)
              const jobId = data?.data?.job_id
              if (!jobId) throw new Error('Backend did not return a job id')
              setShowTruncate(false)
              setTruncateJob({
                jobId, table: selectedTable, status: 'queued',
                phase: 'queued', percent: 0, processed: 0, total: 0,
              })

              // Poll until done/failed (or 5 minutes safety cap)
              const deadline = Date.now() + 300000
              while (Date.now() < deadline) {
                await new Promise(r => setTimeout(r, 700))
                try {
                  const res = await tablesAPI.truncateProgress(jobId)
                  const rec = res.data?.data || {}
                  setTruncateJob({
                    jobId,
                    table: selectedTable,
                    status: rec.status,
                    phase: rec.phase,
                    percent: rec.percent ?? 0,
                    processed: rec.processed ?? 0,
                    total: rec.total ?? 0,
                    method: rec.method,
                    rowsDeleted: rec.rows_deleted,
                    error: rec.error,
                  })
                  if (rec.status === 'done') {
                    toast.success(
                      `Truncated ${rec.rows_deleted ?? 0} rows via ${rec.method || 'TRUNCATE'}`,
                    )
                    break
                  }
                  if (rec.status === 'failed') {
                    toast.error(`Truncate failed: ${rec.error || 'unknown error'}`,
                      { duration: 8000 })
                    break
                  }
                } catch {
                  // transient — keep polling
                }
              }
            } catch (err) {
              toast.error(err.response?.data?.detail || 'Failed to start truncate')
            }
          }}
        />
      )}

      {/* Truncate Progress Modal */}
      {truncateJob && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center">
          <div className="bg-white rounded-lg shadow-xl w-[420px] p-6 space-y-4">
            <div className="flex items-center gap-2">
              <RefreshCw size={18} className={
                truncateJob.status === 'done' || truncateJob.status === 'failed'
                  ? 'text-gray-400'
                  : 'animate-spin text-orange-600'
              } />
              <h3 className="font-semibold text-gray-900">
                Truncating {truncateJob.table}
              </h3>
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <span className="text-gray-600 capitalize">
                  {truncateJob.status === 'done'
                    ? (truncateJob.method === 'truncate'
                        ? 'TRUNCATE TABLE complete'
                        : `Batched DELETE complete`)
                    : truncateJob.status === 'failed'
                      ? 'Failed'
                      : (truncateJob.phase || 'starting…')}
                </span>
                <span className="font-semibold text-gray-900 tabular-nums">
                  {truncateJob.percent}%
                </span>
              </div>
              <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${
                    truncateJob.status === 'failed'
                      ? 'bg-red-500'
                      : truncateJob.status === 'done'
                        ? 'bg-green-500'
                        : 'bg-orange-500'
                  }`}
                  style={{ width: `${truncateJob.percent}%` }}
                />
              </div>
              {truncateJob.total > 0 && (
                <div className="text-xs text-gray-500 tabular-nums">
                  {(truncateJob.processed ?? 0).toLocaleString()} / {truncateJob.total.toLocaleString()} rows
                </div>
              )}
              {truncateJob.error && (
                <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded p-2">
                  {truncateJob.error}
                </div>
              )}
            </div>

            <div className="flex justify-end pt-2">
              <button
                disabled={truncateJob.status !== 'done' && truncateJob.status !== 'failed'}
                onClick={() => {
                  setTruncateJob(null)
                  if (selectedTable) fetchSchema(selectedTable)
                }}
                className={`px-4 py-2 rounded-lg text-sm font-medium ${
                  truncateJob.status === 'done' || truncateJob.status === 'failed'
                    ? 'bg-gray-100 hover:bg-gray-200 text-gray-700'
                    : 'bg-gray-100 text-gray-400 cursor-not-allowed'
                }`}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Drop Table Confirm Modal */}
      {showDrop && (
        <ConfirmModal
          title="Drop Table"
          message={`Are you sure you want to DROP "${selectedTable}"? This action cannot be undone!`}
          confirmText="Drop Table"
          confirmClass="bg-red-600 hover:bg-red-700"
          onClose={() => setShowDrop(false)}
          onConfirm={async () => {
            try {
              await tablesAPI.delete(selectedTable)
              toast.success('Table dropped')
              setShowDrop(false)
              setSelectedTable(null)
              setColumns([])
              fetchTables()
            } catch (err) {
              toast.error(err.response?.data?.detail || 'Failed to drop table')
            }
          }}
        />
      )}
    </div>
  )

  async function handleDeleteColumn(col) {
    const colName = col.column_name || col.name
    if (!confirm(`Delete column "${colName}"? This cannot be undone.`)) return
    try {
      await tablesAPI.alter(selectedTable, { action: 'drop_column', column_name: colName })
      toast.success(`Column "${colName}" deleted`)
      fetchSchema(selectedTable)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to delete column')
    }
  }
}

// Add Column Modal
function AddColumnModal({ table, onClose, onSuccess }) {
  const [form, setForm] = useState({ column_name: '', data_type: 'NVARCHAR', max_length: '255', nullable: true })
  const [saving, setSaving] = useState(false)

  const dataTypes = ['NVARCHAR', 'VARCHAR', 'INT', 'BIGINT', 'DECIMAL', 'FLOAT', 'DATE', 'DATETIME', 'BIT', 'TEXT', 'MONEY']

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!form.column_name.trim()) return toast.error('Column name required')
    setSaving(true)
    try {
      let typeSpec = form.data_type
      if (['NVARCHAR', 'VARCHAR'].includes(form.data_type) && form.max_length) {
        typeSpec = `${form.data_type}(${form.max_length})`
      } else if (form.data_type === 'DECIMAL') {
        typeSpec = 'DECIMAL(18,2)'
      }
      await tablesAPI.alter(table, {
        action: 'add_column',
        column_name: form.column_name.trim(),
        data_type: typeSpec,
        nullable: form.nullable,
      })
      toast.success('Column added')
      onSuccess()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to add column')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">Add Column</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg">×</button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="label">Column Name</label>
            <input value={form.column_name} onChange={e => setForm({ ...form, column_name: e.target.value })} className="input" placeholder="e.g., NEW_COLUMN" required />
          </div>
          <div>
            <label className="label">Data Type</label>
            <select value={form.data_type} onChange={e => setForm({ ...form, data_type: e.target.value })} className="input">
              {dataTypes.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          {['NVARCHAR', 'VARCHAR'].includes(form.data_type) && (
            <div>
              <label className="label">Max Length</label>
              <input type="number" value={form.max_length} onChange={e => setForm({ ...form, max_length: e.target.value })} className="input" placeholder="255" />
            </div>
          )}
          <div className="flex items-center gap-2">
            <input type="checkbox" id="nullable" checked={form.nullable} onChange={e => setForm({ ...form, nullable: e.target.checked })} />
            <label htmlFor="nullable" className="text-sm">Allow NULL values</label>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={saving} className="btn-primary">{saving ? 'Adding...' : 'Add Column'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

// Rename Column Modal
function RenameColumnModal({ table, column, onClose, onSuccess }) {
  const oldName = column.column_name || column.name
  const [newName, setNewName] = useState(oldName)
  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!newName.trim() || newName.trim() === oldName) return onClose()
    setSaving(true)
    try {
      await tablesAPI.alter(table, {
        action: 'rename_column',
        column_name: oldName,
        new_name: newName.trim(),
      })
      toast.success('Column renamed')
      onSuccess()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to rename column')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">Rename Column</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg">×</button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="label">Current Name</label>
            <input value={oldName} className="input bg-gray-50" disabled />
          </div>
          <div>
            <label className="label">New Name</label>
            <input value={newName} onChange={e => setNewName(e.target.value)} className="input" required />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={saving} className="btn-primary">{saving ? 'Renaming...' : 'Rename'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

// Change Type Modal
function ChangeTypeModal({ table, column, onClose, onSuccess }) {
  const colName = column.column_name || column.name
  const currentType = column.data_type || column.type
  const [newType, setNewType] = useState(currentType.toUpperCase())
  const [maxLength, setMaxLength] = useState(column.max_length || column.character_maximum_length || '255')
  const [saving, setSaving] = useState(false)

  const dataTypes = ['NVARCHAR', 'VARCHAR', 'INT', 'BIGINT', 'DECIMAL', 'FLOAT', 'DATE', 'DATETIME', 'BIT', 'TEXT', 'MONEY']

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      let typeSpec = newType
      if (['NVARCHAR', 'VARCHAR'].includes(newType) && maxLength) {
        typeSpec = `${newType}(${maxLength})`
      } else if (newType === 'DECIMAL') {
        typeSpec = 'DECIMAL(18,2)'
      }
      await tablesAPI.alter(table, {
        action: 'alter_column',
        column_name: colName,
        new_type: typeSpec,
      })
      toast.success('Column type changed')
      onSuccess()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to change type')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">Change Data Type</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg">×</button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          <div>
            <label className="label">Column</label>
            <input value={colName} className="input bg-gray-50" disabled />
          </div>
          <div>
            <label className="label">Current Type</label>
            <input value={currentType} className="input bg-gray-50" disabled />
          </div>
          <div>
            <label className="label">New Type</label>
            <select value={newType} onChange={e => setNewType(e.target.value)} className="input">
              {dataTypes.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          {['NVARCHAR', 'VARCHAR'].includes(newType) && (
            <div>
              <label className="label">Max Length</label>
              <input type="number" value={maxLength} onChange={e => setMaxLength(e.target.value)} className="input" />
            </div>
          )}
          <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3 text-sm text-yellow-800">
            <AlertTriangle size={14} className="inline mr-2" />
            Changing data types may cause data loss. Ensure compatibility before proceeding.
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={saving} className="btn-primary">{saving ? 'Changing...' : 'Change Type'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

// Confirm Modal
function ConfirmModal({ title, message, confirmText, confirmClass, onClose, onConfirm }) {
  const [confirming, setConfirming] = useState(false)

  const handleConfirm = async () => {
    setConfirming(true)
    await onConfirm()
    setConfirming(false)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <AlertTriangle className="text-orange-500" size={20} /> {title}
          </h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg">×</button>
        </div>
        <div className="p-6">
          <p className="text-gray-600 mb-6">{message}</p>
          <div className="flex justify-end gap-3">
            <button onClick={onClose} className="btn-secondary">Cancel</button>
            <button onClick={handleConfirm} disabled={confirming} className={`btn-primary ${confirmClass}`}>
              {confirming ? 'Processing...' : confirmText}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
