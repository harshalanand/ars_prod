import { useState } from 'react'
import { X, Plus, Trash2 } from 'lucide-react'
import { tablesAPI } from '@/services/api'
import toast from 'react-hot-toast'

const DATA_TYPES = ['NVARCHAR', 'INT', 'BIGINT', 'DECIMAL', 'FLOAT', 'BIT', 'DATE', 'DATETIME2']

export default function CreateTableModal({ onClose, onCreated }) {
  const [name, setName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [module, setModule] = useState('')
  const [columns, setColumns] = useState([
    { column_name: '', data_type: 'NVARCHAR', max_length: 255, is_primary_key: false, is_nullable: true },
  ])
  const [loading, setLoading] = useState(false)

  const addCol = () => setColumns([...columns, { column_name: '', data_type: 'NVARCHAR', max_length: 255, is_primary_key: false, is_nullable: true }])
  const removeCol = (i) => setColumns(columns.filter((_, idx) => idx !== i))
  const updateCol = (i, field, val) => { const c = [...columns]; c[i] = { ...c[i], [field]: val }; setColumns(c) }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!name.trim()) return toast.error('Table name required')
    if (columns.some(c => !c.column_name.trim())) return toast.error('All columns need names')
    setLoading(true)
    try {
      await tablesAPI.create({ table_name: name.trim(), display_name: displayName || name, module: module || null, columns })
      toast.success('Table created')
      onCreated()
    } catch {} finally { setLoading(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto m-4">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">Create New Table</h2>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded-lg"><X size={18} /></button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-5">
          <div className="grid grid-cols-3 gap-4">
            <div><label className="label">Table Name*</label><input value={name} onChange={e => setName(e.target.value)} className="input" placeholder="store_targets" required /></div>
            <div><label className="label">Display Name</label><input value={displayName} onChange={e => setDisplayName(e.target.value)} className="input" placeholder="Store Targets" /></div>
            <div><label className="label">Module</label><input value={module} onChange={e => setModule(e.target.value)} className="input" placeholder="planning" /></div>
          </div>

          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="label mb-0">Columns</label>
              <button type="button" onClick={addCol} className="btn-ghost btn-sm"><Plus size={14} /> Add Column</button>
            </div>
            <div className="space-y-2">
              {columns.map((col, i) => (
                <div key={i} className="flex items-center gap-2 p-3 bg-gray-50 rounded-lg">
                  <input value={col.column_name} onChange={e => updateCol(i, 'column_name', e.target.value)} className="input flex-1" placeholder="column_name" />
                  <select value={col.data_type} onChange={e => updateCol(i, 'data_type', e.target.value)} className="input w-32">
                    {DATA_TYPES.map(t => <option key={t}>{t}</option>)}
                  </select>
                  {['NVARCHAR','VARCHAR'].includes(col.data_type) && (
                    <input type="number" value={col.max_length || ''} onChange={e => updateCol(i, 'max_length', parseInt(e.target.value) || null)} className="input w-20" placeholder="255" />
                  )}
                  <label className="flex items-center gap-1 text-xs whitespace-nowrap">
                    <input type="checkbox" checked={col.is_primary_key} onChange={e => updateCol(i, 'is_primary_key', e.target.checked)} className="rounded" /> PK
                  </label>
                  <label className="flex items-center gap-1 text-xs whitespace-nowrap">
                    <input type="checkbox" checked={col.is_nullable} onChange={e => updateCol(i, 'is_nullable', e.target.checked)} className="rounded" /> Null
                  </label>
                  {columns.length > 1 && (
                    <button type="button" onClick={() => removeCol(i)} className="p-1 text-gray-400 hover:text-red-500"><Trash2 size={14} /></button>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose} className="btn-secondary">Cancel</button>
            <button type="submit" disabled={loading} className="btn-primary">{loading ? 'Creating...' : 'Create Table'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}
