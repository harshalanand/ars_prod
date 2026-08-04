import { Plus, Trash2 } from 'lucide-react'
import { SAP_OPERATORS, NO_VALUE_OPS, buildWhere, emptyCond } from '@/utils/sapWhere'

// Visual multi-condition WHERE builder. Rows of (field, operator, value) joined
// by AND / OR. Shows a live preview of the generated clause. `conds` is the
// controlled value; `onChange` receives the new array.
export default function WhereBuilder({ conds = [], onChange, fieldSuggestions = [], listId = 'sap-field-list' }) {
  const rows = Array.isArray(conds) ? conds : []
  const set = (i, k, v) => onChange(rows.map((c, idx) => idx === i ? { ...c, [k]: v } : c))
  const add = () => onChange([...rows, emptyCond()])
  const remove = (i) => onChange(rows.filter((_, idx) => idx !== i))
  const preview = buildWhere(rows)

  return (
    <div className="space-y-2">
      {rows.map((c, i) => (
        <div key={i} className="flex items-center gap-2">
          {i > 0 ? (
            <select className="input w-[72px] py-1 text-xs" value={c.conn || 'AND'} onChange={e => set(i, 'conn', e.target.value)}>
              <option>AND</option><option>OR</option>
            </select>
          ) : (
            <span className="w-[72px] text-[11px] text-gray-400 text-center font-semibold">WHERE</span>
          )}
          <input className="input flex-1 font-mono text-xs" list={listId} placeholder="FIELD"
            value={c.field || ''} onChange={e => set(i, 'field', e.target.value.toUpperCase())} />
          <select className="input w-28 py-1 text-xs" value={c.op || '='} onChange={e => set(i, 'op', e.target.value)}>
            {SAP_OPERATORS.map(o => <option key={o} value={o}>{o}</option>)}
          </select>
          <input className="input flex-1 font-mono text-xs disabled:bg-gray-100"
            placeholder={c.op === 'IN' || c.op === 'NOT IN' ? 'A,B,C' : 'value'}
            disabled={NO_VALUE_OPS.includes(c.op)}
            value={c.value || ''} onChange={e => set(i, 'value', e.target.value)} />
          <button type="button" onClick={() => remove(i)} className="p-1 text-red-500 hover:bg-red-50 rounded shrink-0" title="Remove"><Trash2 size={14} /></button>
        </div>
      ))}

      {fieldSuggestions?.length > 0 && (
        <datalist id={listId}>{fieldSuggestions.map(f => <option key={f} value={f} />)}</datalist>
      )}

      <div className="flex items-center justify-between gap-3">
        <button type="button" onClick={add} className="text-xs text-primary-600 hover:underline inline-flex items-center gap-1">
          <Plus size={13} /> Add condition
        </button>
        {preview
          ? <code className="text-[11px] text-gray-500 bg-gray-50 border px-2 py-0.5 rounded truncate" title={preview}>{preview}</code>
          : <span className="text-[11px] text-gray-300">no filter — reads all rows</span>}
      </div>
    </div>
  )
}
