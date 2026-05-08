/**
 * ContribPresetsPage – Manage presets with fast loading.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import { contribAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { Settings, Trash2, Save, RefreshCw, ChevronUp, ChevronDown } from 'lucide-react'
import { C } from '@/theme/colors'

// Fast normalize: just grab first 10 chars (YYYY-MM-DD)
const normMonth = (m) => String(m).substring(0, 10)

export default function ContribPresetsPage() {
  const [presets, setPresets] = useState([])
  const [months, setMonths] = useState([])
  const [loading, setLoading] = useState(false)
  const [form, setForm] = useState({ preset_name:'', months:[], avg_days:30, kpi_type:'L30D', description:'' })
  const [editing, setEditing] = useState(null)

  // Memoize normalized months set for fast lookup
  const monthsSet = useMemo(() => new Set(months), [months])

  // Load months ONCE on mount (cached on backend for 5 min)
  useEffect(() => {
    contribAPI.getMonths().then(r => {
      setMonths((r.data?.data?.months || []).map(normMonth))
    }).catch(() => {})
  }, [])

  // Load presets (fast, no months re-fetch)
  const loadPresets = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await contribAPI.listPresets()
      const list = (data.data?.presets || []).map(p => ({
        ...p,
        months: [...new Set((p.months || []).map(normMonth))].filter(m => monthsSet.size === 0 || monthsSet.has(m))
      }))
      setPresets(list)
    } catch { toast.error('Failed to load presets') }
    finally { setLoading(false) }
  }, [monthsSet])

  useEffect(() => { if (months.length > 0) loadPresets() }, [months, loadPresets])

  const handleSave = async () => {
    if (!form.preset_name.trim()) { toast.error('Name required'); return }
    try {
      const validMonths = [...new Set(form.months.map(normMonth))].filter(m => monthsSet.has(m))
      await contribAPI.savePreset({...form, months: validMonths})
      toast.success(`Preset '${form.preset_name}' saved`)
      setForm({ preset_name:'', months:[], avg_days:30, kpi_type:'L30D', description:'' })
      setEditing(null)
      loadPresets()
    } catch (e) { toast.error(e.response?.data?.detail || 'Save failed') }
  }

  const handleDelete = async (name) => {
    if (!confirm(`Delete preset "${name}"?`)) return
    try { await contribAPI.deletePreset(name); toast.success('Deleted'); loadPresets() }
    catch { toast.error('Delete failed') }
  }

  const handleEdit = (p) => {
    setForm({ preset_name: p.preset_name, months: [...(p.months || [])], avg_days: p.avg_days, kpi_type: p.kpi_type, description: p.description || '' })
    setEditing(p.preset_name)
  }

  const movePreset = async (idx, dir) => {
    const arr = [...presets]
    const newIdx = idx + dir
    if (newIdx < 0 || newIdx >= arr.length) return
    ;[arr[idx], arr[newIdx]] = [arr[newIdx], arr[idx]]
    setPresets(arr)  // Instant UI update
    try { await contribAPI.reorderPresets(arr.map(p => p.preset_name)) }
    catch { toast.error('Reorder failed'); loadPresets() }
  }

  const toggleMonth = (m) => {
    const key = normMonth(m)
    setForm(f => ({...f, months: f.months.includes(key) ? f.months.filter(x=>x!==key) : [...f.months, key]}))
  }

  return (
    <div style={{ color:C.text }}>
      <h1 style={{ fontSize:20, fontWeight:800, margin:'0 0 20px', display:'flex', alignItems:'center', gap:10 }}>
        <Settings size={20} color={C.primary}/> Contribution % — Presets
      </h1>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
        {/* Left: Form */}
        <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, padding:18 }}>
          <div style={{ fontSize:14, fontWeight:700, marginBottom:14 }}>{editing ? `Edit: ${editing}` : 'Create New Preset'}</div>

          <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Preset Name</label>
          <input value={form.preset_name} onChange={e => setForm(f=>({...f, preset_name:e.target.value}))}
            disabled={!!editing}
            style={{ width:'100%', padding:'8px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`, background:C.inputBg, color:C.text, fontSize:13, marginBottom:12, boxSizing:'border-box' }}/>

          <div style={{ display:'flex', gap:12, marginBottom:12 }}>
            <div style={{ flex:1 }}>
              <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Avg Days</label>
              <input type="number" value={form.avg_days} onChange={e => setForm(f=>({...f, avg_days:parseInt(e.target.value)||30}))}
                style={{ width:'100%', padding:'8px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`, background:C.inputBg, color:C.text, fontSize:13, boxSizing:'border-box' }}/>
            </div>
            <div style={{ flex:1 }}>
              <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>KPI Type</label>
              <select value={form.kpi_type} onChange={e => setForm(f=>({...f, kpi_type:e.target.value}))}
                style={{ width:'100%', padding:'8px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`, background:C.inputBg, color:C.text, fontSize:13, boxSizing:'border-box' }}>
                <option value="L30D">L30D</option>
                <option value="L18M">L18M</option>
                <option value="L7D">L7D</option>
              </select>
            </div>
          </div>

          <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Description</label>
          <input value={form.description} onChange={e => setForm(f=>({...f, description:e.target.value}))}
            style={{ width:'100%', padding:'8px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`, background:C.inputBg, color:C.text, fontSize:13, marginBottom:12, boxSizing:'border-box' }}/>

          <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>
            Months ({form.months.length} selected)
          </label>
          <div style={{ display:'flex', flexWrap:'wrap', gap:5, maxHeight:180, overflowY:'auto', marginTop:6, marginBottom:14 }}>
            {months.map(m => {
              const sel = form.months.includes(m)
              return (
                <button key={m} onClick={() => toggleMonth(m)} style={{
                  padding:'4px 10px', borderRadius:16, fontSize:11, fontWeight:600, cursor:'pointer',
                  border:`1.5px solid ${sel ? C.primary : '#e2e8f0'}`,
                  background:sel ? C.primary : '#fff', color:sel ? '#fff' : C.textSub,
                  boxShadow:sel ? '0 2px 6px rgba(79,70,229,.25)' : 'none',
                }}>{m}</button>
              )
            })}
          </div>

          <div style={{ display:'flex', gap:8 }}>
            <button onClick={handleSave} style={{
              flex:1, display:'flex', alignItems:'center', justifyContent:'center', gap:6,
              padding:'10px', borderRadius:8, fontSize:13, fontWeight:700, border:'none', cursor:'pointer',
              background:C.primary, color:'#fff',
            }}><Save size={14}/> {editing ? 'Update' : 'Save'}</button>
            {editing && <button onClick={() => { setEditing(null); setForm({ preset_name:'', months:[], avg_days:30, kpi_type:'L30D', description:'' }) }}
              style={{ padding:'10px 16px', borderRadius:8, fontSize:13, fontWeight:700, border:`1px solid ${C.cardBorder}`, background:'#fff', color:C.textSub, cursor:'pointer' }}>Cancel</button>}
          </div>
        </div>

        {/* Right: Preset list */}
        <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, overflow:'hidden' }}>
          <div style={{ padding:'12px 18px', background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`, display:'flex', justifyContent:'space-between', alignItems:'center' }}>
            <span style={{ fontSize:13, fontWeight:700 }}>{presets.length} Presets (execution order)</span>
            <button onClick={loadPresets} style={{ background:'none', border:'none', cursor:'pointer', padding:4 }}>
              <RefreshCw size={14} color={C.textMuted} style={{ animation:loading?'spin 1s linear infinite':'none' }}/>
            </button>
          </div>
          <div style={{ maxHeight:480, overflowY:'auto' }}>
            {presets.map((p, idx) => (
              <div key={p.preset_name} style={{
                display:'flex', alignItems:'center', gap:8, padding:'10px 14px',
                borderBottom:`1px solid ${C.cardBorder}`, background: editing===p.preset_name ? C.primaryLight : '#fff',
              }}>
                <div style={{ display:'flex', flexDirection:'column', gap:2 }}>
                  <button onClick={() => movePreset(idx,-1)} disabled={idx===0}
                    style={{ background:'none', border:'none', cursor:'pointer', padding:0, opacity:idx===0?0.3:1 }}><ChevronUp size={14} color={C.textMuted}/></button>
                  <button onClick={() => movePreset(idx,1)} disabled={idx===presets.length-1}
                    style={{ background:'none', border:'none', cursor:'pointer', padding:0, opacity:idx===presets.length-1?0.3:1 }}><ChevronDown size={14} color={C.textMuted}/></button>
                </div>
                <span style={{ width:24, height:24, borderRadius:6, background:C.primaryLight, display:'flex', alignItems:'center', justifyContent:'center', fontSize:11, fontWeight:800, color:C.primary, flexShrink:0 }}>{idx+1}</span>
                <div style={{ flex:1, minWidth:0 }}>
                  <div style={{ fontSize:13, fontWeight:700, color:C.text }}>{p.preset_name}</div>
                  <div style={{ fontSize:11, color:C.textMuted }}>{p.kpi_type} · {p.avg_days}d · {p.months?.length || 0} months</div>
                </div>
                <button onClick={() => handleEdit(p)} style={{ padding:'4px 10px', borderRadius:6, fontSize:11, fontWeight:600, border:`1px solid ${C.primaryBd}`, background:C.primaryLight, color:C.primary, cursor:'pointer' }}>Edit</button>
                <button onClick={() => handleDelete(p.preset_name)} style={{ background:'none', border:'none', cursor:'pointer', padding:4 }}><Trash2 size={14} color={C.red}/></button>
              </div>
            ))}
          </div>
        </div>
      </div>
      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
