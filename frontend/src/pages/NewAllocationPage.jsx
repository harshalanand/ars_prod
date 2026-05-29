import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { PackageCheck, ArrowLeft } from 'lucide-react'
import { allocAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { Link } from 'react-router-dom'

export default function NewAllocationPage() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [form, setForm] = useState({
    allocation_name: '',
    allocation_type: 'REPLENISHMENT',
    allocation_basis: 'RATIO',
    category: '',
    season: '',
    warehouse_code: '',
    store_grade_ratios: { A: 1.0, B: 0.7, C: 0.4, D: 0.2 },
    min_per_store: 1,
    max_per_store: null,
    total_qty_limit: null,
    lookback_days: 30,
    notes: '',
  })

  const update = (field, val) => setForm(f => ({ ...f, [field]: val }))
  const updateGrade = (grade, val) => setForm(f => ({
    ...f, store_grade_ratios: { ...f.store_grade_ratios, [grade]: parseFloat(val) || 0 }
  }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!form.allocation_name.trim()) return toast.error('Allocation name required')
    setLoading(true)
    try {
      const payload = {
        ...form,
        max_per_store: form.max_per_store || null,
        total_qty_limit: form.total_qty_limit || null,
      }
      const { data } = await allocAPI.run(payload)
      toast.success('Allocation run complete!')
      navigate(`/allocations/${data.data?.allocation_id || ''}`)
    } catch {} finally { setLoading(false) }
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div className="flex items-center gap-3">
        <Link to="/allocations" className="p-2 hover:bg-gray-100 rounded-lg"><ArrowLeft size={18} /></Link>
        <div>
          <h1 className="text-2xl font-bold text-gray-900">New Allocation</h1>
          <p className="text-gray-500 text-sm">Configure and run a product allocation</p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Basic Info */}
        <div className="card p-6 space-y-4">
          <h3 className="font-semibold text-gray-900">Basic Info</h3>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="label">Allocation Name*</label>
              <input value={form.allocation_name} onChange={e => update('allocation_name', e.target.value)} className="input" placeholder="Spring 2025 Replenishment" required />
            </div>
            <div>
              <label className="label">Type</label>
              <select value={form.allocation_type} onChange={e => update('allocation_type', e.target.value)} className="input">
                {['REPLENISHMENT', 'INITIAL', 'TRANSFER', 'CLEARANCE'].map(t => <option key={t}>{t}</option>)}
              </select>
            </div>
            <div>
              <label className="label">Allocation Basis</label>
              <select value={form.allocation_basis} onChange={e => update('allocation_basis', e.target.value)} className="input">
                {['RATIO', 'SALES', 'STOCK'].map(b => <option key={b}>{b}</option>)}
              </select>
            </div>
            <div>
              <label className="label">Warehouse Code</label>
              <input value={form.warehouse_code} onChange={e => update('warehouse_code', e.target.value)} className="input" placeholder="WH-001" />
            </div>
            <div>
              <label className="label">Category</label>
              <input value={form.category} onChange={e => update('category', e.target.value)} className="input" placeholder="Footwear" />
            </div>
            <div>
              <label className="label">Season</label>
              <input value={form.season} onChange={e => update('season', e.target.value)} className="input" placeholder="SS25" />
            </div>
          </div>
        </div>

        {/* Constraints */}
        <div className="card p-6 space-y-4">
          <h3 className="font-semibold text-gray-900">Constraints</h3>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="label">Min Per Store</label>
              <input type="number" value={form.min_per_store} onChange={e => update('min_per_store', parseInt(e.target.value) || 0)} className="input" min={0} />
            </div>
            <div>
              <label className="label">Max Per Store (optional)</label>
              <input type="number" value={form.max_per_store || ''} onChange={e => update('max_per_store', parseInt(e.target.value) || null)} className="input" placeholder="No limit" />
            </div>
            <div>
              <label className="label">Total Qty Limit (optional)</label>
              <input type="number" value={form.total_qty_limit || ''} onChange={e => update('total_qty_limit', parseInt(e.target.value) || null)} className="input" placeholder="No limit" />
            </div>
          </div>
          {form.allocation_basis === 'SALES' && (
            <div className="max-w-xs">
              <label className="label">Lookback Days</label>
              <input type="number" value={form.lookback_days} onChange={e => update('lookback_days', parseInt(e.target.value) || 30)} className="input" />
            </div>
          )}
        </div>

        {/* Grade Ratios */}
        {form.allocation_basis === 'RATIO' && (
          <div className="card p-6 space-y-4">
            <h3 className="font-semibold text-gray-900">Store Grade Ratios</h3>
            <p className="text-sm text-gray-500">Set allocation ratio multiplier per store grade</p>
            <div className="grid grid-cols-4 gap-4">
              {['A', 'B', 'C', 'D'].map(g => (
                <div key={g}>
                  <label className="label">Grade {g}</label>
                  <input type="number" step="0.1" value={form.store_grade_ratios[g]} onChange={e => updateGrade(g, e.target.value)} className="input" min={0} max={5} />
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Notes */}
        <div className="card p-6">
          <label className="label">Notes (optional)</label>
          <textarea value={form.notes} onChange={e => update('notes', e.target.value)} className="input" rows={3} placeholder="Additional notes..." />
        </div>

        <div className="flex gap-3">
          <Link to="/allocations" className="btn-secondary">Cancel</Link>
          <button type="submit" disabled={loading} className="btn-primary">
            <PackageCheck size={16} /> {loading ? 'Running Allocation...' : 'Run Allocation'}
          </button>
        </div>
      </form>
    </div>
  )
}
