/**
 * ContribMappingsPage – Manage SSN→suffix mappings and mapping assignments.
 * SSN values from DB dropdown, preset names as multi-select chips.
 */
import { useState, useEffect } from 'react'
import { contribAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { Columns, Plus, Trash2, Save, RefreshCw, Link2 } from 'lucide-react'
import { C } from '@/theme/colors'
const inp = { width:'100%', padding:'8px 12px', borderRadius:8, border:`1px solid ${C.inputBorder}`, background:C.inputBg, color:C.text, fontSize:13, boxSizing:'border-box' }

export default function ContribMappingsPage() {
  const [mappings, setMappings] = useState([])
  const [assignments, setAssignments] = useState([])
  const [presets, setPresets] = useState([])
  const [ssnValues, setSsnValues] = useState([])
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState('mappings')

  // Mapping form
  const [mForm, setMForm] = useState({ mapping_name:'', suffix_mapping:{}, fallback_suffixes:[], description:'' })
  const [ssnKey, setSsnKey] = useState('')
  const [ssnSelectedPresets, setSsnSelectedPresets] = useState([])

  // Assignment form
  const [aForm, setAForm] = useState({ col_name:'', mapping_name:'', prefix:'INITIAL AUTO CONT%|', target:'Both' })

  const load = async () => {
    setLoading(true)
    try {
      const [m, a, p, s] = await Promise.all([
        contribAPI.listMappings(), contribAPI.listAssignments(),
        contribAPI.listPresets(), contribAPI.getSsnValues(),
      ])
      setMappings(m.data?.data?.mappings || [])
      setAssignments(a.data?.data?.assignments || [])
      setPresets(p.data?.data?.presets || [])
      setSsnValues(s.data?.data?.ssn_values || [])
    } catch { toast.error('Failed to load') }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const presetNames = presets.map(p => p.preset_name)

  // --- Mapping CRUD ---
  const addSsnPair = () => {
    if (!ssnKey) { toast.error('Select an SSN value'); return }
    if (!ssnSelectedPresets.length) { toast.error('Select at least one preset'); return }
    setMForm(f => ({...f, suffix_mapping:{...f.suffix_mapping, [ssnKey]: [...ssnSelectedPresets]}}))
    setSsnKey(''); setSsnSelectedPresets([])
  }
  const removeSsnPair = (key) => {
    setMForm(f => { const m={...f.suffix_mapping}; delete m[key]; return {...f, suffix_mapping:m} })
  }
  const toggleSsnPreset = (p) => {
    setSsnSelectedPresets(prev => prev.includes(p) ? prev.filter(x=>x!==p) : [...prev, p])
  }
  const toggleFallback = (p) => {
    setMForm(f => ({...f, fallback_suffixes: f.fallback_suffixes.includes(p) ? f.fallback_suffixes.filter(x=>x!==p) : [...f.fallback_suffixes, p]}))
  }

  const saveMapping = async () => {
    if (!mForm.mapping_name.trim()) { toast.error('Name required'); return }
    try {
      await contribAPI.saveMapping(mForm)
      toast.success('Mapping saved')
      setMForm({ mapping_name:'', suffix_mapping:{}, fallback_suffixes:[], description:'' })
      setSsnKey(''); setSsnSelectedPresets([])
      load()
    } catch { toast.error('Save failed') }
  }
  const editMapping = (m) => {
    setMForm({...m})
    setSsnKey(''); setSsnSelectedPresets([])
  }
  const deleteMapping = async (name) => {
    if (!confirm(`Delete mapping "${name}"?`)) return
    try { await contribAPI.deleteMapping(name); toast.success('Deleted'); load() }
    catch { toast.error('Failed') }
  }

  // --- Assignment CRUD ---
  const saveAssignment = async () => {
    if (!aForm.col_name.trim() || !aForm.mapping_name) { toast.error('Fill all fields'); return }
    try {
      await contribAPI.saveAssignment(aForm)
      toast.success('Assignment saved')
      setAForm({ col_name:'', mapping_name:'', prefix:'INITIAL AUTO CONT%|', target:'Both' })
      load()
    } catch { toast.error('Save failed') }
  }
  const deleteAssignment = async (id) => {
    try { await contribAPI.deleteAssignment(id); toast.success('Deleted'); load() }
    catch { toast.error('Failed') }
  }

  // SSN values not yet used in current mapping
  const usedSsns = Object.keys(mForm.suffix_mapping)
  const availableSsns = ssnValues.filter(s => !usedSsns.includes(s))

  return (
    <div style={{ color:C.text }}>
      <h1 style={{ fontSize:20, fontWeight:800, margin:'0 0 16px', display:'flex', alignItems:'center', gap:10 }}>
        <Columns size={20} color={C.primary}/> Contribution % — Mappings & Assignments
      </h1>

      {/* Tabs */}
      <div style={{ display:'flex', gap:4, marginBottom:16, background:'#fff', border:`1px solid ${C.cardBorder}`, borderRadius:8, padding:3, width:'fit-content' }}>
        {[{k:'mappings',l:'SSN Mappings'},{k:'assignments',l:'Assignments'}].map(t => (
          <button key={t.k} onClick={() => setTab(t.k)} style={{
            padding:'6px 18px', borderRadius:6, fontSize:13, fontWeight:700, border:'none', cursor:'pointer',
            background:tab===t.k?C.primary:'transparent', color:tab===t.k?'#fff':C.textSub,
          }}>{t.l}</button>
        ))}
      </div>

      {tab === 'mappings' && (
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
          {/* Create/Edit Mapping */}
          <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, padding:18 }}>
            <div style={{ fontSize:14, fontWeight:700, marginBottom:14 }}>
              {mForm.mapping_name && mappings.some(m=>m.mapping_name===mForm.mapping_name) ? `Edit: ${mForm.mapping_name}` : 'Create Mapping'}
            </div>

            <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Mapping Name</label>
            <input value={mForm.mapping_name} onChange={e => setMForm(f=>({...f, mapping_name:e.target.value}))} style={{...inp, marginBottom:10}}/>

            <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Description</label>
            <input value={mForm.description} onChange={e => setMForm(f=>({...f, description:e.target.value}))} style={{...inp, marginBottom:14}}/>

            {/* SSN → Preset Suffixes */}
            <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase', marginBottom:6, display:'block' }}>
              SSN → Preset Suffixes
            </label>

            {/* SSN Dropdown */}
            <div style={{ background:'#f8fafc', border:`1px solid ${C.cardBorder}`, borderRadius:10, padding:12, marginBottom:10 }}>
              <div style={{ display:'flex', gap:8, marginBottom:8 }}>
                <div style={{ flex:1 }}>
                  <label style={{ fontSize:10, fontWeight:600, color:C.textMuted, textTransform:'uppercase' }}>SSN Value</label>
                  <select value={ssnKey} onChange={e => setSsnKey(e.target.value)}
                    style={{...inp, marginTop:3}}>
                    <option value="">-- select SSN --</option>
                    {availableSsns.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                </div>
              </div>

              {/* Preset multi-select chips */}
              <label style={{ fontSize:10, fontWeight:600, color:C.textMuted, textTransform:'uppercase' }}>Select Presets for this SSN</label>
              <div style={{ display:'flex', flexWrap:'wrap', gap:5, marginTop:4, marginBottom:8 }}>
                {presetNames.map(p => {
                  const sel = ssnSelectedPresets.includes(p)
                  return (
                    <button key={p} onClick={() => toggleSsnPreset(p)} style={{
                      padding:'4px 12px', borderRadius:16, fontSize:11, fontWeight:600, cursor:'pointer',
                      border:`1.5px solid ${sel ? C.primary : '#e2e8f0'}`,
                      background:sel ? C.primary : '#fff', color:sel ? '#fff' : C.textSub,
                      transition:'all .12s',
                    }}>{sel ? '✓ ' : ''}{p}</button>
                  )
                })}
              </div>

              <button onClick={addSsnPair} disabled={!ssnKey || !ssnSelectedPresets.length} style={{
                display:'flex', alignItems:'center', gap:5, padding:'7px 16px', borderRadius:8, fontSize:12, fontWeight:700,
                border:'none', cursor: (!ssnKey || !ssnSelectedPresets.length) ? 'not-allowed' : 'pointer',
                background: (!ssnKey || !ssnSelectedPresets.length) ? '#e2e8f0' : C.primary, color: (!ssnKey || !ssnSelectedPresets.length) ? C.textMuted : '#fff',
              }}><Plus size={13}/> Add SSN Rule</button>
            </div>

            {/* Current SSN rules */}
            {Object.keys(mForm.suffix_mapping).length > 0 && (
              <div style={{ marginBottom:14 }}>
                <label style={{ fontSize:10, fontWeight:600, color:C.textMuted, textTransform:'uppercase' }}>
                  Current Rules ({Object.keys(mForm.suffix_mapping).length})
                </label>
                <div style={{ maxHeight:150, overflowY:'auto', marginTop:4 }}>
                  {Object.entries(mForm.suffix_mapping).map(([k,v]) => (
                    <div key={k} style={{ display:'flex', alignItems:'center', gap:8, padding:'6px 10px', background:C.primaryLight, borderRadius:8, marginBottom:4 }}>
                      <span style={{ fontWeight:800, color:C.primary, fontSize:13, minWidth:50 }}>{k}</span>
                      <span style={{ color:C.textMuted, fontSize:12 }}>→</span>
                      <div style={{ flex:1, display:'flex', flexWrap:'wrap', gap:3 }}>
                        {(v||[]).map(s => (
                          <span key={s} style={{ padding:'2px 8px', borderRadius:12, fontSize:10, fontWeight:700, background:'#fff', color:C.primary, border:`1px solid ${C.primaryBd}` }}>{s}</span>
                        ))}
                      </div>
                      <button onClick={() => removeSsnPair(k)} style={{ background:'none', border:'none', cursor:'pointer', flexShrink:0 }}>
                        <Trash2 size={13} color={C.red}/>
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Fallback Suffixes — also as preset chips */}
            <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase', marginBottom:4, display:'block' }}>
              Fallback Presets ({mForm.fallback_suffixes.length} selected)
            </label>
            <div style={{ display:'flex', flexWrap:'wrap', gap:5, marginBottom:14 }}>
              {presetNames.map(p => {
                const sel = mForm.fallback_suffixes.includes(p)
                return (
                  <button key={p} onClick={() => toggleFallback(p)} style={{
                    padding:'4px 12px', borderRadius:16, fontSize:11, fontWeight:600, cursor:'pointer',
                    border:`1.5px solid ${sel ? C.amber : '#e2e8f0'}`,
                    background:sel ? C.amberBg : '#fff', color:sel ? C.amber : C.textSub,
                    transition:'all .12s',
                  }}>{sel ? '✓ ' : ''}{p}</button>
                )
              })}
            </div>

            <div style={{ display:'flex', gap:8 }}>
              <button onClick={saveMapping} style={{
                flex:1, padding:'10px', borderRadius:8, fontSize:13, fontWeight:700, border:'none', cursor:'pointer',
                background:C.primary, color:'#fff', display:'flex', alignItems:'center', justifyContent:'center', gap:6
              }}><Save size={14}/> Save Mapping</button>
              {mForm.mapping_name && mappings.some(m=>m.mapping_name===mForm.mapping_name) && (
                <button onClick={() => { setMForm({ mapping_name:'', suffix_mapping:{}, fallback_suffixes:[], description:'' }); setSsnKey(''); setSsnSelectedPresets([]) }}
                  style={{ padding:'10px 16px', borderRadius:8, fontSize:13, fontWeight:700, border:`1px solid ${C.cardBorder}`, background:'#fff', color:C.textSub, cursor:'pointer' }}>Cancel</button>
              )}
            </div>
          </div>

          {/* Mapping List */}
          <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, overflow:'hidden' }}>
            <div style={{ padding:'12px 18px', background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`, display:'flex', justifyContent:'space-between', alignItems:'center' }}>
              <span style={{ fontSize:13, fontWeight:700 }}>{mappings.length} Mappings</span>
              <button onClick={load} style={{ background:'none', border:'none', cursor:'pointer' }}>
                <RefreshCw size={14} color={C.textMuted} style={{ animation:loading?'spin 1s linear infinite':'none' }}/>
              </button>
            </div>
            <div style={{ maxHeight:500, overflowY:'auto' }}>
              {mappings.map(m => (
                <div key={m.mapping_name} style={{ padding:'12px 14px', borderBottom:`1px solid ${C.cardBorder}` }}>
                  <div style={{ display:'flex', justifyContent:'space-between', alignItems:'start' }}>
                    <div style={{ flex:1 }}>
                      <div style={{ fontSize:14, fontWeight:700 }}>{m.mapping_name}</div>
                      {m.description && <div style={{ fontSize:11, color:C.textMuted, marginTop:2 }}>{m.description}</div>}
                      <div style={{ marginTop:6 }}>
                        {Object.entries(m.suffix_mapping).map(([k,v]) => (
                          <div key={k} style={{ display:'flex', alignItems:'center', gap:6, fontSize:11, marginBottom:3 }}>
                            <span style={{ fontWeight:700, color:C.primary, minWidth:40 }}>{k}</span>
                            <span style={{ color:C.textMuted }}>→</span>
                            {(v||[]).map(s => (
                              <span key={s} style={{ padding:'1px 6px', borderRadius:10, fontSize:10, fontWeight:600, background:C.primaryLight, color:C.primary, border:`1px solid ${C.primaryBd}` }}>{s}</span>
                            ))}
                          </div>
                        ))}
                      </div>
                      {m.fallback_suffixes?.length > 0 && (
                        <div style={{ marginTop:4, fontSize:11, color:C.amber }}>
                          Fallback: {m.fallback_suffixes.join(', ')}
                        </div>
                      )}
                    </div>
                    <div style={{ display:'flex', gap:6, flexShrink:0, marginLeft:8 }}>
                      <button onClick={() => editMapping(m)} style={{ padding:'4px 10px', borderRadius:6, fontSize:11, fontWeight:600, border:`1px solid ${C.primaryBd}`, background:C.primaryLight, color:C.primary, cursor:'pointer' }}>Edit</button>
                      <button onClick={() => deleteMapping(m.mapping_name)} style={{ background:'none', border:'none', cursor:'pointer' }}><Trash2 size={14} color={C.red}/></button>
                    </div>
                  </div>
                </div>
              ))}
              {mappings.length === 0 && <div style={{ padding:30, textAlign:'center', color:C.textMuted, fontSize:13 }}>No mappings yet</div>}
            </div>
          </div>
        </div>
      )}

      {tab === 'assignments' && (
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
          {/* Assignment Form */}
          <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, padding:18 }}>
            <div style={{ fontSize:14, fontWeight:700, marginBottom:14, display:'flex', alignItems:'center', gap:6 }}>
              <Link2 size={14} color={C.primary}/> New Assignment
            </div>

            <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Output Column Name</label>
            <input value={aForm.col_name} onChange={e => setAForm(f=>({...f, col_name:e.target.value}))} style={{...inp, marginBottom:10}}/>

            <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Mapping</label>
            <select value={aForm.mapping_name} onChange={e => setAForm(f=>({...f, mapping_name:e.target.value}))} style={{...inp, marginBottom:10}}>
              <option value="">-- select --</option>
              {mappings.map(m => <option key={m.mapping_name} value={m.mapping_name}>{m.mapping_name}</option>)}
            </select>

            <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Prefix</label>
            <input value={aForm.prefix} onChange={e => setAForm(f=>({...f, prefix:e.target.value}))} style={{...inp, marginBottom:10}}/>

            <label style={{ fontSize:11, fontWeight:700, color:C.textSub, textTransform:'uppercase' }}>Target</label>
            <select value={aForm.target} onChange={e => setAForm(f=>({...f, target:e.target.value}))} style={{...inp, marginBottom:14}}>
              <option value="Both">Both (Store + Company)</option>
              <option value="Store">Store only</option>
              <option value="Company">Company only</option>
            </select>

            <button onClick={saveAssignment} style={{
              width:'100%', padding:'10px', borderRadius:8, fontSize:13, fontWeight:700, border:'none', cursor:'pointer',
              background:C.green, color:'#fff', display:'flex', alignItems:'center', justifyContent:'center', gap:6
            }}><Plus size={14}/> Add Assignment</button>
          </div>

          {/* Assignment List */}
          <div style={{ background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:12, overflow:'hidden' }}>
            <div style={{ padding:'12px 18px', background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`, fontSize:13, fontWeight:700 }}>
              {assignments.length} Assignments
            </div>
            {assignments.map(a => (
              <div key={a.id} style={{ display:'flex', alignItems:'center', gap:10, padding:'10px 14px', borderBottom:`1px solid ${C.cardBorder}` }}>
                <div style={{ flex:1 }}>
                  <div style={{ fontSize:13, fontWeight:700 }}>{a.col_name}</div>
                  <div style={{ fontSize:11, color:C.textMuted }}>Mapping: {a.mapping_name} · Target: {a.target}</div>
                  <div style={{ fontSize:10, color:C.textMuted }}>Prefix: {a.prefix}</div>
                </div>
                <button onClick={() => deleteAssignment(a.id)} style={{ background:'none', border:'none', cursor:'pointer' }}><Trash2 size={14} color={C.red}/></button>
              </div>
            ))}
            {assignments.length === 0 && <div style={{ padding:30, textAlign:'center', color:C.textMuted, fontSize:13 }}>No assignments yet</div>}
          </div>
        </div>
      )}
      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
