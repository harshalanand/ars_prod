/**
 * GridBuilderPage — Dynamic Pivot Grid Builder
 * Light theme matching ARS app (bg-gray-50 layout).
 */
import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { gridBuilderAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Plus, Play, PlayCircle, Trash2, Edit3, X, Save, Eye,
  CheckCircle2, XCircle, Clock, AlertTriangle, Loader,
  LayoutGrid, ChevronDown, ChevronUp, RefreshCw, Database
} from 'lucide-react'
import { C } from '@/theme/colors'

/* ── tiny helpers ─────────────────────────────────────────────────────────── */
const StatusBadge = ({ s }) => {
  const map = {
    Active:   [C.green,  C.greenBg,  C.greenBd],
    Inactive: [C.red,    C.redBg,    C.redBd],
    Success:  [C.green,  C.greenBg,  C.greenBd],
    Failed:   [C.red,    C.redBg,    C.redBd],
    Running:     [C.blue,   C.blueBg,   C.blueBd],
    Interrupted: ['#f59e0b','#fef3c7','#fde68a'],
  }
  const [col, bg, bd] = map[s] || [C.gray, C.grayBg, C.grayBd]
  return (
    <span style={{ display:'inline-flex', alignItems:'center', gap:4,
      padding:'2px 9px', borderRadius:20, fontSize:11, fontWeight:700,
      background:bg, color:col, border:`1px solid ${bd}`, whiteSpace:'nowrap' }}>
      {s === 'Running' && <Loader size={9} style={{ animation:'spin 1s linear infinite' }} />}
      {s}
    </span>
  )
}

const Btn = ({ onClick, disabled, color='primary', children, style={} }) => {
  const map = {
    primary: { bg:C.primary,  text:'#fff',    bd:C.primary  },
    green:   { bg:C.greenBg,  text:C.green,   bd:C.greenBd  },
    red:     { bg:C.redBg,    text:C.red,     bd:C.redBd    },
    amber:   { bg:C.amberBg,  text:C.amber,   bd:C.amberBd  },
    gray:    { bg:C.grayBg,   text:C.textSub, bd:C.grayBd   },
    blue:    { bg:C.blueBg,   text:C.blue,    bd:C.blueBd   },
  }
  const t = map[color] || map.primary
  return (
    <button onClick={onClick} disabled={disabled}
      style={{ display:'inline-flex', alignItems:'center', gap:6,
        padding:'7px 14px', borderRadius:8, fontSize:12, fontWeight:600,
        cursor: disabled ? 'not-allowed' : 'pointer',
        border:`1px solid ${t.bd}`, background:t.bg, color:t.text,
        opacity: disabled ? .5 : 1, transition:'all .15s', ...style }}>
      {children}
    </button>
  )
}

const Field = ({ label, children, required }) => (
  <div style={{ display:'flex', flexDirection:'column', gap:4 }}>
    <label style={{ fontSize:12, fontWeight:600, color:C.textSub }}>
      {label}{required && <span style={{ color:C.red }}> *</span>}
    </label>
    {children}
  </div>
)

const Input = ({ value, onChange, placeholder, ...rest }) => (
  <input value={value} onChange={onChange} placeholder={placeholder} {...rest}
    style={{ padding:'7px 11px', borderRadius:7, fontSize:13,
      background:C.inputBg, border:`1px solid ${C.inputBd}`,
      color:C.text, outline:'none', fontFamily:'inherit', ...rest.style }} />
)

/* ── Column multi-selector ────────────────────────────────────────────────── */
const ColPicker = ({ available, selected, onChange }) => {
  const toggle = (col) => {
    if (selected.includes(col)) onChange(selected.filter(c => c !== col))
    else onChange([...selected, col])
  }
  const moveUp   = (i) => { if (i === 0) return; const a = [...selected]; [a[i-1],a[i]]=[a[i],a[i-1]]; onChange(a) }
  const moveDown = (i) => { if (i === selected.length-1) return; const a=[...selected]; [a[i],a[i+1]]=[a[i+1],a[i]]; onChange(a) }
  const remove   = (col) => onChange(selected.filter(c => c !== col))

  return (
    <div style={{ border:`1px solid ${C.cardBorder}`, borderRadius:8, overflow:'hidden' }}>
      {/* Available columns */}
      <div style={{ padding:10, background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}` }}>
        <div style={{ fontSize:11, fontWeight:600, color:C.textSub, marginBottom:6 }}>
          Available columns (click to add)
        </div>
        <div style={{ display:'flex', flexWrap:'wrap', gap:5 }}>
          {available.filter(c => !selected.includes(c)).map(col => (
            <button key={col} onClick={() => toggle(col)}
              style={{ padding:'3px 10px', borderRadius:6, fontSize:11, fontWeight:600,
                cursor:'pointer', background:C.primaryLt, color:C.primary,
                border:`1px solid ${C.primaryBd}` }}>
              + {col}
            </button>
          ))}
          {available.filter(c => !selected.includes(c)).length === 0 &&
            <span style={{ fontSize:11, color:C.textMuted }}>All columns selected</span>}
        </div>
      </div>

      {/* Selected (ordered) */}
      <div style={{ padding:10 }}>
        <div style={{ fontSize:11, fontWeight:600, color:C.textSub, marginBottom:6 }}>
          Selected hierarchy (drag order matters for GROUP BY)
        </div>
        {selected.length === 0 ? (
          <div style={{ fontSize:12, color:C.textMuted, fontStyle:'italic' }}>
            No columns selected — default: MATNR, WERKS
          </div>
        ) : selected.map((col, i) => (
          <div key={col} style={{ display:'flex', alignItems:'center', gap:6,
            padding:'5px 8px', borderRadius:6, background:C.grayBg,
            border:`1px solid ${C.cardBorder}`, marginBottom:4 }}>
            <span style={{ flex:1, fontSize:12, fontWeight:600, color:C.text, fontFamily:'monospace' }}>{col}</span>
            <button onClick={() => moveUp(i)} disabled={i===0}
              style={{ border:'none', background:'none', cursor: i===0 ? 'not-allowed' : 'pointer',
                color: i===0 ? C.textMuted : C.primary, padding:'1px 3px' }}>
              <ChevronUp size={13}/>
            </button>
            <button onClick={() => moveDown(i)} disabled={i===selected.length-1}
              style={{ border:'none', background:'none', cursor: i===selected.length-1 ? 'not-allowed' : 'pointer',
                color: i===selected.length-1 ? C.textMuted : C.primary, padding:'1px 3px' }}>
              <ChevronDown size={13}/>
            </button>
            <button onClick={() => remove(col)}
              style={{ border:'none', background:'none', cursor:'pointer', color:C.red, padding:'1px 3px' }}>
              <X size={13}/>
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ── Create / Edit Modal ──────────────────────────────────────────────────── */
const EMPTY_FORM = { grid_name:'', description:'', hierarchy_columns:[], kpi_filter:'', output_table:'', status:'Active', pivot_only:false, weightage:1.0, grid_group:'Primary', use_for_opt_sale:false, sec_cap_applicable:false, sec_cap_pct:null }

const GridModal = ({ open, onClose, onSave, availableCols, editing, allGrids = [] }) => {
  const [form, setForm] = useState(EMPTY_FORM)

  // Find the grid (if any, besides the one being edited) that already owns use_for_opt_sale
  const existingOptSaleGrid = allGrids.find(g =>
    !!g.use_for_opt_sale && (!editing || g.id !== editing.id)
  )
  const optSaleLocked = !!existingOptSaleGrid && !form.use_for_opt_sale

  useEffect(() => {
    if (editing) setForm({
      ...editing,
      hierarchy_columns: editing.hierarchy_columns || [],
      weightage:  editing.weightage  ?? 1.0,         // null/undefined → 1.0
      grid_group: editing.grid_group || 'Primary',   // null/empty → 'Primary'
      use_for_opt_sale:   !!editing.use_for_opt_sale,
      sec_cap_applicable: !!editing.sec_cap_applicable,
      sec_cap_pct:        editing.sec_cap_pct ?? null,
    })
    else setForm(EMPTY_FORM)
  }, [editing, open])

  const set = (k,v) => setForm(p => ({ ...p, [k]: v }))

  // Auto-generate output table name from grid name
  const autoTable = (name) => {
    const safe = name.toUpperCase().replace(/[^A-Z0-9]/g, '_').replace(/^_+|_+$/g,'')
    return safe ? `ARS_GRID_${safe}` : ''
  }

  const handleNameChange = (v) => {
    set('grid_name', v)
    if (!editing) set('output_table', autoTable(v))
  }

  const handleSave = async () => {
    if (!form.grid_name.trim()) { toast.error('Grid name is required'); return }
    if (!form.output_table.trim()) { toast.error('Output table is required'); return }
    await onSave(form)
  }

  if (!open) return null

  return (
    <div style={{ position:'fixed', inset:0, background:'rgba(0,0,0,.5)',
      display:'flex', alignItems:'center', justifyContent:'center', zIndex:1000 }}>
      <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:14,
        width:'min(700px, 95vw)', maxHeight:'90vh', overflow:'auto',
        boxShadow:'0 20px 60px rgba(0,0,0,.2)' }}>

        {/* Modal header */}
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
          padding:'16px 20px', borderBottom:`1px solid ${C.cardBorder}`, background:C.headerBg }}>
          <h2 style={{ margin:0, fontSize:16, fontWeight:700, color:C.text }}>
            {editing ? `Edit Grid: ${editing.grid_name}` : 'Create New Grid'}
          </h2>
          <button onClick={onClose} style={{ border:'none', background:'none',
            cursor:'pointer', color:C.textSub, padding:4 }}><X size={18}/></button>
        </div>

        {/* Modal body */}
        <div style={{ padding:20, display:'flex', flexDirection:'column', gap:16 }}>

          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:14 }}>
            <Field label="Grid Name" required>
              <Input value={form.grid_name} onChange={e => handleNameChange(e.target.value)}
                placeholder="e.g. STK Summary" />
            </Field>
            <Field label="Status">
              <select value={form.status} onChange={e => set('status', e.target.value)}
                style={{ padding:'7px 11px', borderRadius:7, fontSize:13,
                  background:C.inputBg, border:`1px solid ${C.inputBd}`, color:C.text, outline:'none' }}>
                <option value="Active">Active</option>
                <option value="Inactive">Inactive</option>
              </select>
            </Field>
          </div>

          <Field label="Description">
            <Input value={form.description || ''} onChange={e => set('description', e.target.value)}
              placeholder="Optional description" />
          </Field>

          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:14 }}>
            <Field label="KPI Filter"
              title="Leave blank to include all active SLOCs. Enter a KPI value (e.g. STK) to only include SLOCs where KPI matches.">
              <Input value={form.kpi_filter || ''} onChange={e => set('kpi_filter', e.target.value)}
                placeholder="e.g. STK (leave blank for all)" />
              <span style={{ fontSize:10, color:C.textMuted }}>
                Filters ARS_STORE_SLOC_SETTINGS.KPI column
              </span>
            </Field>
            <Field label="Output Table" required>
              <Input value={form.output_table || ''} onChange={e => set('output_table', e.target.value.toUpperCase())}
                placeholder="e.g. ARS_GRID_STK" style={{ fontFamily:'monospace', fontSize:12 }} />
              <span style={{ fontSize:10, color:C.textMuted }}>
                Created/truncated on each run in Rep_data
              </span>
            </Field>
            <Field label="Pivot Only">
              <label style={{ display:'flex', alignItems:'center', gap:6, cursor:'pointer', fontSize:11 }}>
                <input type="checkbox" checked={!!form.pivot_only} onChange={e => set('pivot_only', e.target.checked)}
                  style={{ width:14, height:14 }} />
                Skip lookups &amp; calculations (CONT, MBQ, OPT_CNT)
              </label>
              <span style={{ fontSize:10, color:C.textMuted }}>
                Enable for article-level grids that only need the pivot output
              </span>
            </Field>
            <Field label="Weightage">
              <input type="number" step="0.1" min="0" value={form.weightage ?? 1.0}
                onChange={e => set('weightage', parseFloat(e.target.value) || 0)}
                style={{ width:'100%', padding:'6px 10px', borderRadius:6, border:`1px solid ${C.inputBd}`,
                  fontSize:12, background:C.inputBg }} placeholder="1.0" />
              <span style={{ fontSize:10, color:C.textMuted }}>Priority weight for this grid (higher = more important)</span>
            </Field>
            <Field label="Grid Group">
              <select value={form.grid_group || 'Primary'}
                onChange={e => set('grid_group', e.target.value)}
                style={{ width:'100%', padding:'6px 10px', borderRadius:6, border:`1px solid ${C.inputBd}`,
                  fontSize:12, background:C.inputBg }}>
                <option value="None">None</option>
                <option value="Primary">Primary</option>
                <option value="Secondary">Secondary</option>
              </select>
              <span style={{ fontSize:10, color:C.textMuted }}>Classification: Primary grids are core, Secondary are supplementary</span>
            </Field>
            <Field label="Use for PER_OPT_SALE">
              <label style={{
                display:'flex', alignItems:'center', gap:6, fontSize:11,
                cursor: optSaleLocked ? 'not-allowed' : 'pointer',
                opacity: optSaleLocked ? 0.55 : 1,
              }}>
                <input type="checkbox" checked={!!form.use_for_opt_sale}
                  disabled={optSaleLocked}
                  onChange={e => set('use_for_opt_sale', e.target.checked)}
                  style={{ width:14, height:14, cursor: optSaleLocked ? 'not-allowed' : 'pointer' }} />
                Use this grid's MBQ &amp; DISP_Q for listing PER_OPT_SALE
              </label>
              {optSaleLocked ? (
                <span style={{ fontSize:10, color:C.red, fontWeight:600 }}>
                  🔒 Locked — already assigned to grid:{' '}
                  <strong>{existingOptSaleGrid?.grid_name}</strong>. Uncheck it there first to reassign.
                </span>
              ) : (
                <span style={{ fontSize:10, color:C.textMuted }}>
                  Only ONE grid can be selected for PER_OPT_SALE source.
                  Formula: ((MBQ − DISP_Q) / DISP_Q × ACS_D) / ALC_D
                </span>
              )}
            </Field>
            {/* Sec-cap participation — only meaningful for non-pivot Secondary grids. */}
            <Field label="Apply Sec-Cap">
              {(() => {
                const secCapLocked = (form.grid_group !== 'Secondary') || !!form.pivot_only
                return (
                  <>
                    <label style={{
                      display:'flex', alignItems:'center', gap:6, fontSize:11,
                      cursor: secCapLocked ? 'not-allowed' : 'pointer',
                      opacity: secCapLocked ? 0.55 : 1,
                    }}>
                      <input type="checkbox"
                        checked={!!form.sec_cap_applicable && !secCapLocked}
                        disabled={secCapLocked}
                        onChange={e => set('sec_cap_applicable', e.target.checked)}
                        style={{ width:14, height:14, cursor: secCapLocked ? 'not-allowed' : 'pointer' }} />
                      Cap this grid during allocation
                    </label>
                    {secCapLocked ? (
                      <span style={{ fontSize:10, color:C.textMuted }}>
                        🔒 Locked — only non-pivot Secondary grids can participate in sec-cap.
                      </span>
                    ) : (
                      <span style={{ fontSize:10, color:C.textMuted }}>
                        When ON, allocation enforces this grid's MBQ × cap%. Default cap = 130%.
                      </span>
                    )}
                  </>
                )
              })()}
            </Field>
            <Field label="Sec-Cap %">
              {(() => {
                const secCapLocked = (form.grid_group !== 'Secondary') || !!form.pivot_only
                const pctDisabled = secCapLocked || !form.sec_cap_applicable
                return (
                  <>
                    <input type="number" step="1" min="0" max="500"
                      value={form.sec_cap_pct ?? ''}
                      disabled={pctDisabled}
                      onChange={e => {
                        const v = e.target.value
                        set('sec_cap_pct', v === '' ? null : parseFloat(v))
                      }}
                      placeholder="blank → use global 130"
                      style={{ width:'100%', padding:'6px 10px', borderRadius:6,
                        border:`1px solid ${C.inputBd}`, fontSize:12,
                        background: pctDisabled ? C.grayBg : C.inputBg,
                        opacity: pctDisabled ? 0.6 : 1 }} />
                    <span style={{ fontSize:10, color:C.textMuted }}>
                      {pctDisabled
                        ? 'Enable “Apply Sec-Cap” first.'
                        : 'Per-grid override. Blank = use the global default (130%).'}
                    </span>
                  </>
                )
              })()}
            </Field>
          </div>

          <Field label="Hierarchy Columns (from vw_master_product)">
            <ColPicker
              available={availableCols}
              selected={form.hierarchy_columns}
              onChange={v => set('hierarchy_columns', v)}
            />
          </Field>

        </div>

        {/* Modal footer */}
        <div style={{ display:'flex', justifyContent:'flex-end', gap:10,
          padding:'14px 20px', borderTop:`1px solid ${C.cardBorder}`, background:C.headerBg }}>
          <Btn onClick={onClose} color="gray"><X size={13}/> Cancel</Btn>
          <Btn onClick={handleSave} color="primary"><Save size={13}/> {editing ? 'Save Changes' : 'Create Grid'}</Btn>
        </div>
      </div>
    </div>
  )
}

/* ── Run Results Modal ────────────────────────────────────────────────────── */
const RunResultsModal = ({ results, onClose }) => {
  if (!results) return null
  return (
    <div style={{ position:'fixed', inset:0, background:'rgba(0,0,0,.5)',
      display:'flex', alignItems:'center', justifyContent:'center', zIndex:1000 }}>
      <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:14,
        width:'min(560px, 95vw)', maxHeight:'80vh', overflow:'auto',
        boxShadow:'0 20px 60px rgba(0,0,0,.2)' }}>
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
          padding:'16px 20px', borderBottom:`1px solid ${C.cardBorder}`, background:C.headerBg }}>
          <h2 style={{ margin:0, fontSize:16, fontWeight:700, color:C.text }}>Run All — Results</h2>
          <button onClick={onClose} style={{ border:'none', background:'none', cursor:'pointer', color:C.textSub }}><X size={18}/></button>
        </div>
        <div style={{ padding:20 }}>
          {results.map((r, i) => (
            <div key={i} style={{ display:'flex', alignItems:'center', justifyContent:'space-between',
              padding:'10px 14px', borderRadius:8, marginBottom:8,
              background: r.status === 'Success' ? C.greenBg : C.redBg,
              border:`1px solid ${r.status === 'Success' ? C.greenBd : C.redBd}` }}>
              <div>
                <div style={{ fontWeight:700, color:C.text, fontSize:13 }}>{r.grid_name}</div>
                {r.error && <div style={{ fontSize:11, color:C.red, marginTop:2 }}>{r.error}</div>}
              </div>
              <div style={{ textAlign:'right' }}>
                <StatusBadge s={r.status}/>
                {r.status === 'Success' && (
                  <div style={{ fontSize:11, color:C.textSub, marginTop:3 }}>{r.rows.toLocaleString()} rows</div>
                )}
              </div>
            </div>
          ))}
        </div>
        <div style={{ padding:'12px 20px', borderTop:`1px solid ${C.cardBorder}`, textAlign:'right' }}>
          <Btn onClick={onClose} color="gray"><X size={13}/> Close</Btn>
        </div>
      </div>
    </div>
  )
}

/* ── Sec-Cap Growth Matrix panel ─────────────────────────────────────────── */
/* Editable cont%-band table with a global on/off toggle. When on, allocation
 * replaces the flat per-grid sec_cap_pct with a per-grain growth% resolved
 * from these bands (spec 2026-07-08). Half-open [lo, hi); last row hi=null
 * for open-ended. Growth% must be >= 100 (matrix relaxes, never tightens).
 */
const DEFAULT_BANDS = [
  { lo: 0,  hi: 5,    growth: 300 },
  { lo: 5,  hi: 10,   growth: 250 },
  { lo: 10, hi: 15,   growth: 200 },
  { lo: 15, hi: 30,   growth: 150 },
  { lo: 30, hi: null, growth: 120 },
]

function validateBandsClient(bands) {
  const errors = []
  const warnings = []
  if (!bands || bands.length === 0) {
    errors.push('At least one band is required')
    return { errors, warnings }
  }
  const nums = bands.map((b, i) => {
    const lo = Number(b.lo)
    const hi = b.hi === null || b.hi === '' ? null : Number(b.hi)
    const g  = Number(b.growth)
    if (Number.isNaN(lo)) errors.push(`Row ${i+1}: lo must be numeric`)
    if (Number.isNaN(g))  errors.push(`Row ${i+1}: growth must be numeric`)
    if (hi !== null && Number.isNaN(hi)) errors.push(`Row ${i+1}: hi must be numeric or empty`)
    if (lo < 0) errors.push(`Row ${i+1}: lo must be >= 0`)
    if (g < 100) errors.push(`Row ${i+1}: growth must be >= 100`)
    return { lo, hi, g }
  })
  if (errors.length) return { errors, warnings }
  // Sorted by lo
  for (let i = 1; i < nums.length; i++) {
    if (nums[i].lo < nums[i-1].lo) errors.push('Bands must be sorted ascending by lo')
  }
  // Exactly one null hi, must be last
  const nullCount = nums.filter(n => n.hi === null).length
  if (nullCount !== 1) errors.push(`Exactly one row must have hi=empty (found ${nullCount})`)
  else if (nums[nums.length-1].hi !== null) errors.push('The empty-hi row must be last')
  // Contiguous
  for (let i = 0; i < nums.length - 1; i++) {
    if (nums[i].hi === null) { errors.push(`Row ${i+1}: only the last row may have hi empty`); break }
    if (Math.abs(nums[i].hi - nums[i+1].lo) > 1e-9) {
      errors.push(`Row ${i+1}: hi (${nums[i].hi}) must equal next row's lo (${nums[i+1].lo})`)
    }
  }
  if (errors.length === 0) {
    const monotonic = nums.every((n, i) => i === 0 || nums[i-1].g >= n.g)
    if (!monotonic) warnings.push('Growth% is not monotonically non-increasing — small contributors may not always get a larger stretch.')
  }
  return { errors, warnings }
}

function resolveGrowthClient(contPct, bands, fallback) {
  for (const b of bands) {
    const hi = b.hi === null || b.hi === '' ? null : Number(b.hi)
    const lo = Number(b.lo)
    const g  = Number(b.growth)
    if (hi === null) { if (contPct >= lo) return { growth: g, matched: true } }
    else if (contPct >= lo && contPct < hi) return { growth: g, matched: true }
  }
  return { growth: fallback, matched: false }
}

function GrowthMatrixPanel() {
  const [expanded, setExpanded] = useState(false)
  const [loading,  setLoading]  = useState(false)
  const [saving,   setSaving]   = useState(false)
  const [enabled,  setEnabled]  = useState(false)
  const [bands,    setBands]    = useState([])
  const [previewCont, setPreviewCont] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await gridBuilderAPI.getGrowthMatrix()
      setEnabled(!!data.data?.enabled)
      setBands((data.data?.bands || []).map(b => ({
        lo: b.lo, hi: b.hi, growth: b.growth,
      })))
    } catch (e) {
      toast.error('Failed to load growth matrix')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { if (expanded) load() }, [expanded, load])

  const { errors, warnings } = validateBandsClient(bands)
  const canSave = !saving && errors.length === 0 && bands.length > 0
  const canEnable = bands.length > 0

  const updateRow = (idx, key, val) => {
    setBands(prev => prev.map((b, i) => i === idx
      ? { ...b, [key]: val === '' ? (key === 'hi' ? null : '') : Number(val) }
      : b
    ))
  }
  const deleteRow = (idx) => {
    setBands(prev => prev.filter((_, i) => i !== idx))
  }
  const addRow = () => {
    setBands(prev => {
      if (prev.length === 0) return [{ lo: 0, hi: null, growth: 100 }]
      // Insert before the open-ended row; split its lo→new_hi at a sensible midpoint
      const last = prev[prev.length - 1]
      const secondLastHi = prev.length >= 2 ? prev[prev.length - 2].hi : 0
      const newLo = Number(secondLastHi) || Number(last.lo) || 0
      const newHi = newLo + 5
      // Update the previously-last row's lo to newHi so contiguity holds
      const updated = [...prev.slice(0, -1),
        { lo: newLo, hi: newHi, growth: 100 },
        { ...last, lo: newHi },
      ]
      return updated
    })
  }
  const resetToDefaults = () => setBands(DEFAULT_BANDS.map(b => ({ ...b })))

  const handleSave = async () => {
    const { errors: eList } = validateBandsClient(bands)
    if (eList.length) { toast.error(eList[0]); return }
    if (enabled && bands.length === 0) {
      toast.error('Add at least one band before enabling')
      return
    }
    setSaving(true)
    try {
      const payload = {
        enabled: !!enabled,
        bands: bands.map(b => ({
          lo: Number(b.lo),
          hi: b.hi === null || b.hi === '' ? null : Number(b.hi),
          growth: Number(b.growth),
        })),
      }
      const { data } = await gridBuilderAPI.saveGrowthMatrix(payload)
      const w = data?.data?.warnings || []
      if (w.length) toast(w[0], { icon: '⚠️' })
      else toast.success('Growth matrix saved')
    } catch (e) {
      const detail = e?.response?.data?.detail
      const first = typeof detail === 'string' ? detail
        : Array.isArray(detail?.errors) ? detail.errors[0] : 'Save failed'
      toast.error(first)
    } finally {
      setSaving(false)
    }
  }

  const preview = (() => {
    const cp = Number(previewCont)
    if (previewCont === '' || Number.isNaN(cp)) return null
    return resolveGrowthClient(cp, bands, 120)
  })()

  return (
    <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:12,
      marginBottom:16, overflow:'hidden', boxShadow:'0 1px 3px rgba(0,0,0,.08)' }}>
      <div onClick={() => setExpanded(v => !v)}
        style={{ display:'flex', alignItems:'center', gap:10, padding:'12px 18px',
          cursor:'pointer', background:C.headerBg, borderBottom: expanded ? `1px solid ${C.cardBorder}` : 'none' }}>
        {expanded ? <ChevronUp size={16} color={C.textSub}/> : <ChevronDown size={16} color={C.textSub}/>}
        <span style={{ fontSize:13, fontWeight:600, color:C.text }}>Sec-Cap Growth Matrix</span>
        <span style={{ fontSize:11, color:C.textSub }}>
          cont%-driven per-grain ceiling override
        </span>
        <span style={{ marginLeft:'auto', fontSize:11, color: enabled ? C.green : C.textSub, fontWeight:600 }}>
          {loading ? 'Loading…' : (enabled ? 'ENABLED' : 'disabled')}
        </span>
      </div>

      {expanded && (
        <div style={{ padding:'14px 18px' }}>
          <div style={{ display:'flex', alignItems:'center', gap:12, marginBottom:12 }}>
            <label style={{ display:'inline-flex', alignItems:'center', gap:8, fontSize:13, color:C.text }}>
              <input type="checkbox" checked={!!enabled} disabled={!canEnable}
                onChange={e => setEnabled(e.target.checked)}
                title={canEnable ? '' : 'Add at least one band before enabling'} />
              Enabled
            </label>
            <span style={{ fontSize:11, color:C.textSub }}>
              OFF → each grid's own sec_cap_pct is used (no change to today's behavior).
            </span>
            <div style={{ marginLeft:'auto', display:'flex', gap:8 }}>
              <Btn onClick={resetToDefaults} color="gray">Reset to defaults</Btn>
              <Btn onClick={handleSave} disabled={!canSave} color="primary">
                {saving ? <Loader size={13} style={{ animation:'spin 1s linear infinite' }}/> : <Save size={13}/>}
                Save
              </Btn>
            </div>
          </div>

          <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12 }}>
            <thead>
              <tr style={{ background:'#f1f5f9', borderBottom:`2px solid ${C.cardBorder}` }}>
                {['#','cont % from','cont % to','growth %','actions'].map(h => (
                  <th key={h} style={{ padding:'6px 10px', textAlign:'left',
                    fontSize:10, fontWeight:700, color:C.textSub,
                    textTransform:'uppercase', letterSpacing:'.04em' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bands.map((b, i) => (
                <tr key={i} style={{ borderBottom:`1px solid ${C.cardBorder}` }}>
                  <td style={{ padding:'6px 10px', color:C.textSub }}>{i+1}</td>
                  <td style={{ padding:'6px 10px' }}>
                    <Input value={b.lo ?? ''} onChange={e => updateRow(i, 'lo', e.target.value)}
                      style={{ width:100 }} type="number" />
                  </td>
                  <td style={{ padding:'6px 10px' }}>
                    <Input value={b.hi === null ? '' : (b.hi ?? '')}
                      onChange={e => updateRow(i, 'hi', e.target.value)}
                      placeholder={b.hi === null ? '(open)' : ''}
                      style={{ width:100 }} type="number" />
                  </td>
                  <td style={{ padding:'6px 10px' }}>
                    <Input value={b.growth ?? ''} onChange={e => updateRow(i, 'growth', e.target.value)}
                      style={{ width:100 }} type="number" />
                  </td>
                  <td style={{ padding:'6px 10px' }}>
                    <button onClick={() => deleteRow(i)}
                      style={{ background:'transparent', border:'none', cursor:'pointer', color:C.red }}
                      title="Delete band">
                      <Trash2 size={14}/>
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ marginTop:8 }}>
            <Btn onClick={addRow} color="gray">
              <Plus size={12}/> Add band
            </Btn>
          </div>

          {errors.length > 0 && (
            <div style={{ marginTop:10, padding:'8px 12px', background:C.redBg,
              border:`1px solid ${C.redBd}`, borderRadius:6, fontSize:11, color:C.red }}>
              {errors.map((e, i) => <div key={i}>• {e}</div>)}
            </div>
          )}
          {errors.length === 0 && warnings.length > 0 && (
            <div style={{ marginTop:10, padding:'8px 12px', background:C.amberBg,
              border:`1px solid ${C.amberBd}`, borderRadius:6, fontSize:11, color:C.amber }}>
              {warnings.map((w, i) => <div key={i}>• {w}</div>)}
            </div>
          )}

          <div style={{ marginTop:14, padding:'10px 12px', background:C.grayBg,
            border:`1px solid ${C.grayBd}`, borderRadius:6, display:'flex',
            alignItems:'center', gap:10, fontSize:12 }}>
            <span style={{ fontWeight:600, color:C.text }}>Preview:</span>
            <span style={{ color:C.textSub }}>at cont =</span>
            <Input value={previewCont} onChange={e => setPreviewCont(e.target.value)}
              placeholder="e.g. 13" type="number" style={{ width:80 }} />
            <span style={{ color:C.textSub }}>%,</span>
            {preview ? (
              preview.matched
                ? <span>growth = <strong>{preview.growth}%</strong> × MBQ</span>
                : <span style={{ color:C.amber }}>no band matched — fallback {preview.growth}%</span>
            ) : (
              <span style={{ color:C.textMuted }}>enter a cont% to resolve</span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/* ── Main Page ────────────────────────────────────────────────────────────── */
export default function GridBuilderPage() {
  const navigate = useNavigate()
  const [grids,       setGrids]      = useState([])
  const [availCols,   setAvailCols]  = useState(['MATNR','WERKS'])
  const [loading,     setLoading]    = useState(false)
  const [runningId,   setRunningId]  = useState(null)   // grid id currently running
  const [runningAll,  setRunningAll] = useState(false)
  const [modalOpen,   setModalOpen]  = useState(false)
  const [editing,     setEditing]    = useState(null)
  const [seqChanged,  setSeqChanged] = useState(false)
  const [savingSeq,   setSavingSeq]  = useState(false)
  const [runResults,  setRunResults] = useState(null)
  const [deleteConf,  setDeleteConf] = useState(null)   // id to confirm delete
  const [calcLog,     setCalcLog]    = useState(null)   // calculation steps modal
  const [buildingCalc, setBuildingCalc] = useState(false) // build calc tables in progress

  /* load */
  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [gRes, cRes] = await Promise.all([
        gridBuilderAPI.listGrids(),
        gridBuilderAPI.getColumns(),
      ])
      setGrids(gRes.data.data.grids || [])
      setAvailCols(cRes.data.data.columns || ['MATNR','WERKS'])
      setSeqChanged(false)
    } catch {} finally { setLoading(false) }
  }, [])
  useEffect(() => { load() }, [load])

  /* sequence management */
  const moveGrid = (idx, dir) => {
    const newIdx = idx + dir
    if (newIdx < 0 || newIdx >= grids.length) return
    const updated = [...grids]
    const [moved] = updated.splice(idx, 1)
    updated.splice(newIdx, 0, moved)
    setGrids(updated)
    setSeqChanged(true)
  }

  const saveSequence = async () => {
    setSavingSeq(true)
    try {
      const sequence = grids.map((g, i) => ({ id: g.id, seq: i + 1 }))
      await gridBuilderAPI.reorder(sequence)
      setSeqChanged(false)
      toast.success('Sequence saved')
    } catch { toast.error('Failed to save sequence') }
    finally { setSavingSeq(false) }
  }

  /* create / update */
  const handleSave = async (form) => {
    try {
      let res
      if (editing) {
        res = await gridBuilderAPI.updateGrid(editing.id, form)
        toast.success(`Grid '${form.grid_name || editing.grid_name}' updated.`)
      } else {
        res = await gridBuilderAPI.createGrid(form)
        toast.success(`Grid '${form.grid_name}' created.`)
      }
      const warns = res?.data?.data?.warnings || []
      if (warns.length) {
        toast(warns.join('\n'), { icon: '⚠️', duration: 6000, style: { fontSize: 11, maxWidth: 400 } })
      }
      setModalOpen(false); setEditing(null)
      await load()
    } catch {}
  }

  /* toggle status */
  const handleToggleStatus = async (grid) => {
    const newStatus = grid.status === 'Active' ? 'Inactive' : 'Active'
    try {
      await gridBuilderAPI.updateGrid(grid.id, { status: newStatus })
      toast.success(`Grid '${grid.grid_name}' marked ${newStatus}.`)
      await load()
    } catch {}
  }

  /* inline sec-cap toggle / % edit (optimistic update) */
  const handleSecCapPatch = async (grid, patch) => {
    setGrids(prev => prev.map(g => g.id === grid.id ? { ...g, ...patch } : g))
    try {
      await gridBuilderAPI.updateGrid(grid.id, patch)
    } catch {
      // rollback on failure
      setGrids(prev => prev.map(g => g.id === grid.id ? grid : g))
      toast.error('Failed to update sec-cap')
    }
  }

  /* delete */
  const handleDelete = async (id) => {
    try {
      await gridBuilderAPI.deleteGrid(id)
      toast.success('Grid deleted.')
      setDeleteConf(null)
      await load()
    } catch {}
  }

  /* poll grid list while running */
  const pollGrids = (interval = 2000) => {
    const tid = setInterval(async () => {
      try {
        const res = await gridBuilderAPI.listGrids()
        setGrids(res.data.data.grids || [])
      } catch {}
    }, interval)
    return () => clearInterval(tid)
  }

  /* run single */
  const handleRun = async (grid) => {
    setRunningId(grid.id)
    setGrids(prev => prev.map(g => g.id === grid.id ? { ...g, last_run_status: 'Running' } : g))
    const stopPoll = pollGrids()
    try {
      const { data } = await gridBuilderAPI.runGrid(grid.id)
      const warns = data.data?.warnings || []
      if (warns.length) {
        toast(warns.join('\n'), { icon: '⚠️', duration: 6000, style: { fontSize: 11, maxWidth: 400 } })
      }
      toast.success(data.message)
      await load()
    } catch {} finally { stopPoll(); setRunningId(null) }
  }

  /* build calc tables */
  const handleBuildCalc = async () => {
    setBuildingCalc(true)
    try {
      const { data } = await gridBuilderAPI.buildCalcTables()
      toast.success(data.message)
      setCalcLog({ steps: data.data?.steps || [], duration: data.data?.duration || 0 })
    } catch { toast.error('Build calc tables failed') }
    finally { setBuildingCalc(false) }
  }

  /* run all — backend now spawns the work in a background thread and returns
     in ~1s (so it fits inside Cloudflare's 120s proxy timeout). We poll
     /run-all/status until the server reports running:false, refreshing the
     grid table on every tick so per-grid progress is live. */
  const handleRunAll = async () => {
    setRunningAll(true)
    setGrids(prev => prev.map(g => g.status === 'Active' ? { ...g, last_run_status: 'Running' } : g))
    try {
      const { data } = await gridBuilderAPI.runAll()
      // Backend rejects with success=false if a previous run is still going
      if (data.success === false) {
        toast.error(data.message || 'Run All already in progress')
        setRunningAll(false)
        return
      }
      toast.success(data.message || 'Run All started')

      // Poll loop — refresh grids every 3s; stop when server says not running
      const POLL_MS  = 3000
      const MAX_TIME = 30 * 60 * 1000   // 30-minute safety cap
      const start    = Date.now()
      while (Date.now() - start < MAX_TIME) {
        await new Promise(r => setTimeout(r, POLL_MS))
        try {
          const [gridsRes, statusRes] = await Promise.all([
            gridBuilderAPI.listGrids(),
            gridBuilderAPI.runAllStatus(),
          ])
          setGrids(gridsRes.data.data.grids || [])
          if (statusRes.data.data?.running === false) {
            const last = statusRes.data.data?.last_results || []
            setRunResults(last)
            const ok = last.filter(r => r.status === 'Success').length
            toast.success(`Run All complete: ${ok}/${last.length} grids succeeded`)
            break
          }
        } catch {/* transient — keep polling */}
      }
      await load()
    } catch { toast.error('Run All failed to start') }
    finally { setRunningAll(false) }
  }

  const activeCount = grids.filter(g => g.status === 'Active').length

  /* ── render ──────────────────────────────────────────────────────────── */
  return (
    <div style={{ color:C.text, fontFamily:'inherit' }}>
      {/* Page title */}
      <div style={{ marginBottom:20 }}>
        <h1 style={{ fontSize:18, fontWeight:700, color:C.text, margin:0,
          display:'flex', alignItems:'center', gap:8 }}>
          <LayoutGrid size={20} color={C.primary}/>
          Store Stock Grid Builder
        </h1>
        <p style={{ fontSize:13, color:C.textSub, marginTop:4 }}>
          Build dynamic pivot grids from{' '}
          <code style={{ fontSize:11, background:C.primaryLt, color:C.primary,
            padding:'1px 6px', borderRadius:4, border:`1px solid ${C.primaryBd}` }}>
            ET_STORE_STOCK
          </code>
          {' '}joined with{' '}
          <code style={{ fontSize:11, background:C.primaryLt, color:C.primary,
            padding:'1px 6px', borderRadius:4, border:`1px solid ${C.primaryBd}` }}>
            vw_master_product
          </code>
          . Each run creates / truncates / inserts into the output table.
        </p>
      </div>

      {/* Sec-Cap Growth Matrix (collapsed by default) */}
      <GrowthMatrixPanel />

      {/* Main card */}
      <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:12,
        overflow:'hidden', boxShadow:'0 1px 3px rgba(0,0,0,.08)' }}>

        {/* Card header */}
        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
          flexWrap:'wrap', gap:10, padding:'14px 18px',
          background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}` }}>
          <span style={{ fontSize:13, fontWeight:600, color:C.textSub }}>
            {grids.length} grid{grids.length!==1?'s':''} &nbsp;·&nbsp;
            <span style={{ color:C.green }}>{activeCount} active</span>
          </span>
          <div style={{ display:'flex', gap:8, flexWrap:'wrap' }}>
            {seqChanged && (
              <Btn onClick={saveSequence} disabled={savingSeq} color="amber">
                {savingSeq ? <Loader size={13} style={{ animation:'spin 1s linear infinite' }}/> : <Save size={13}/>}
                Save Sequence
              </Btn>
            )}
            <Btn onClick={() => { setEditing(null); setModalOpen(true) }} color="primary">
              <Plus size={13}/> New Grid
            </Btn>
            <Btn onClick={handleRunAll} disabled={runningAll || activeCount===0} color="green">
              {runningAll
                ? <><Loader size={13} style={{ animation:'spin 1s linear infinite' }}/> Running…</>
                : <><PlayCircle size={13}/> Run All Active ({activeCount})</>}
            </Btn>
            <Btn onClick={handleBuildCalc} disabled={buildingCalc} color="amber">
              {buildingCalc
                ? <><Loader size={13} style={{ animation:'spin 1s linear infinite' }}/> Building…</>
                : <><Database size={13}/> Build Calc Tables</>}
            </Btn>
            <Btn onClick={async () => {
              try {
                const { data } = await gridBuilderAPI.calcPreview()
                setCalcLog({ steps: data.data?.steps || [], duration: data.data?.duration || 0 })
              } catch { toast.error('Failed to load calc log') }
            }} color="blue">
              <Database size={13}/> Calc Log
            </Btn>
            <Btn onClick={load} disabled={loading} color="gray">
              <RefreshCw size={13} style={{ animation:loading?'spin 1s linear infinite':'none' }}/>
            </Btn>
          </div>
        </div>

        {/* Grid list */}
        {loading ? (
          <div style={{ textAlign:'center', padding:60, color:C.textMuted }}>
            <RefreshCw size={20} style={{ display:'block', margin:'0 auto 8px',
              animation:'spin 1s linear infinite' }}/>
            Loading grids…
          </div>
        ) : grids.length === 0 ? (
          <div style={{ textAlign:'center', padding:60, color:C.textMuted }}>
            <LayoutGrid size={32} style={{ display:'block', margin:'0 auto 10px', opacity:.3 }}/>
            No grids yet. Click <strong>New Grid</strong> to create one.
          </div>
        ) : (
          <div style={{ overflowX:'auto' }}>
            <table style={{ width:'100%', borderCollapse:'collapse', fontSize:10, minWidth:700 }}>
              <thead>
                <tr style={{ background:'#f1f5f9', borderBottom:`2px solid ${C.cardBorder}` }}>
                  {['#','Grid Name','Output Table','KPI','Group','Sec-Cap','Wt',
                    'Last Run','Status','Rows','Time','Alerts','Actions'].map(h => (
                    <th key={h} style={{ padding:'5px 8px', textAlign:'left',
                      fontSize:9, fontWeight:700, color:C.textSub,
                      textTransform:'uppercase', letterSpacing:'.04em',
                      whiteSpace:'nowrap' }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {grids.map((g, idx) => {
                  const isRunning = runningId === g.id
                  return (
                    <tr key={g.id} style={{
                      borderBottom:`1px solid ${C.cardBorder}`,
                      background: idx%2===0 ? C.card : C.rowAlt,
                    }}>
                      {/* Sequence */}
                      <td style={{ padding:'4px 4px', textAlign:'center', width:40 }}>
                        <div style={{ display:'flex', alignItems:'center', gap:1, justifyContent:'center' }}>
                          <div style={{ display:'flex', flexDirection:'column' }}>
                            <button onClick={() => moveGrid(idx, -1)} disabled={idx === 0}
                              style={{ background:'none', border:'none', cursor: idx === 0 ? 'default' : 'pointer', padding:0, opacity: idx === 0 ? .2 : .6, lineHeight:0 }}>
                              <ChevronUp size={10} />
                            </button>
                            <button onClick={() => moveGrid(idx, 1)} disabled={idx === grids.length - 1}
                              style={{ background:'none', border:'none', cursor: idx === grids.length - 1 ? 'default' : 'pointer', padding:0, opacity: idx === grids.length - 1 ? .2 : .6, lineHeight:0 }}>
                              <ChevronDown size={10} />
                            </button>
                          </div>
                          <span style={{ fontSize:10, fontWeight:700, color:C.textMuted }}>{idx + 1}</span>
                        </div>
                      </td>
                      {/* Grid name */}
                      <td style={{ padding:'4px 8px' }}>
                        <div style={{ fontWeight:700, fontSize:11, color:C.text }}>{g.grid_name}</div>
                      </td>

                      {/* Output table */}
                      <td style={{ padding:'4px 8px' }}>
                        <code style={{ fontSize:9, color:C.primary, background:C.primaryLt,
                          padding:'1px 5px', borderRadius:3, border:`1px solid ${C.primaryBd}`,
                          fontFamily:'monospace', fontWeight:600 }}>
                          {g.output_table}
                        </code>
                      </td>

                      {/* KPI filter */}
                      <td style={{ padding:'4px 8px' }}>
                        <div style={{ display:'flex', gap:3, alignItems:'center' }}>
                          {g.kpi_filter
                            ? <span style={{ fontSize:9, fontWeight:700, color:C.amber,
                                background:C.amberBg, border:`1px solid ${C.amberBd}`,
                                padding:'1px 5px', borderRadius:3 }}>{g.kpi_filter}</span>
                            : <span style={{ fontSize:9, color:C.textMuted }}>All</span>}
                          {g.pivot_only && <span style={{ fontSize:7, fontWeight:700, color:'#7c3aed',
                            background:'#ede9fe', border:'1px solid #c4b5fd',
                            padding:'0px 4px', borderRadius:3 }}>PIVOT ONLY</span>}
                          {g.use_for_opt_sale && <span style={{ fontSize:7, fontWeight:700, color:C.green,
                            background:C.greenBg, border:`1px solid ${C.greenBd}`,
                            padding:'0px 4px', borderRadius:3 }} title="Source for PER_OPT_SALE">OPT_SALE</span>}
                        </div>
                      </td>

                      {/* Grid Group */}
                      <td style={{ padding:'4px 8px' }}>
                        {g.grid_group && g.grid_group !== 'None' ? (
                          <span style={{ fontSize:9, fontWeight:600,
                            color: g.grid_group === 'Secondary' ? C.amber : C.primary,
                            background: g.grid_group === 'Secondary' ? C.amberBg : C.primaryLt,
                            border: `1px solid ${g.grid_group === 'Secondary' ? C.amberBd : C.primaryBd}`,
                            padding:'1px 5px', borderRadius:3 }}>
                            {g.grid_group}
                          </span>
                        ) : (
                          <span style={{ fontSize:9, color:C.textMuted }}>—</span>
                        )}
                      </td>

                      {/* Sec-Cap (click toggle + editable %) */}
                      <td style={{ padding:'4px 8px', textAlign:'center', whiteSpace:'nowrap' }}>
                        {(g.grid_group !== 'Secondary' || g.pivot_only) ? (
                          <span style={{ fontSize:9, color:C.textMuted }}
                            title="Only non-pivot Secondary grids support sec-cap">—</span>
                        ) : (
                          <div style={{ display:'inline-flex', alignItems:'center', gap:4 }}>
                            <button
                              onClick={() => handleSecCapPatch(g, { sec_cap_applicable: !g.sec_cap_applicable })}
                              title={g.sec_cap_applicable ? 'Click to turn OFF' : 'Click to turn ON'}
                              style={{
                                display:'inline-flex', alignItems:'center', gap:4,
                                padding:'2px 7px', borderRadius:10, fontSize:9, fontWeight:700,
                                cursor:'pointer', border:'none',
                                background: g.sec_cap_applicable ? C.greenBg : C.grayBg,
                                color:      g.sec_cap_applicable ? C.green   : C.textSub }}>
                              <span style={{ width:20, height:10, borderRadius:5, position:'relative',
                                display:'inline-block', flexShrink:0,
                                background: g.sec_cap_applicable ? '#10b981' : '#e2e8f0' }}>
                                <span style={{ position:'absolute', top:1, width:8, height:8,
                                  borderRadius:'50%', background:'#fff',
                                  boxShadow:'0 1px 2px rgba(0,0,0,.3)',
                                  left: g.sec_cap_applicable ? 10 : 2 }}/>
                              </span>
                              {g.sec_cap_applicable ? 'ON' : 'OFF'}
                            </button>
                            {g.sec_cap_applicable && (
                              <span style={{ display:'inline-flex', alignItems:'center', gap:1 }}>
                                <input
                                  className="sec-cap-pct"
                                  type="number" min="0" max="500" step="1"
                                  defaultValue={g.sec_cap_pct ?? ''}
                                  placeholder="130"
                                  title="Per-grid cap %. Blank = global default (130%)."
                                  onKeyDown={e => { if (e.key === 'Enter') e.target.blur() }}
                                  onBlur={e => {
                                    const raw = e.target.value
                                    const next = raw === '' ? null : parseFloat(raw)
                                    const cur  = g.sec_cap_pct ?? null
                                    if (next !== cur) handleSecCapPatch(g, { sec_cap_pct: next })
                                  }}
                                  style={{ width:58, padding:'2px 6px', fontSize:10, fontWeight:600,
                                    textAlign:'right', borderRadius:4, border:`1px solid ${C.inputBd}`,
                                    background:C.inputBg, color:C.text, outline:'none',
                                    MozAppearance:'textfield' }} />
                                <span style={{ fontSize:9, fontWeight:600, color:C.textSub }}>%</span>
                              </span>
                            )}
                          </div>
                        )}
                      </td>

                      {/* Weightage */}
                      <td style={{ padding:'4px 8px', textAlign:'center' }}>
                        <span style={{ fontSize:10, fontWeight:600, color:C.text }}>
                          {g.weightage != null ? g.weightage : 1.0}
                        </span>
                      </td>

                      {/* Last run */}
                      <td style={{ padding:'4px 8px', whiteSpace:'nowrap' }}>
                        {g.last_run_at ? (
                          <div style={{ fontSize:9, color:C.textSub }}>
                            {new Date(g.last_run_at).toLocaleDateString()},{' '}
                            {new Date(g.last_run_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}
                          </div>
                        ) : (
                          <span style={{ fontSize:9, color:C.textMuted }}>—</span>
                        )}
                      </td>

                      {/* Status */}
                      <td style={{ padding:'4px 8px' }}>
                        <div style={{ display:'flex', alignItems:'center', gap:4 }}>
                          <button onClick={() => handleToggleStatus(g)} style={{
                            display:'inline-flex', alignItems:'center', gap:4,
                            padding:'2px 8px', borderRadius:10, fontSize:9, fontWeight:700,
                            cursor:'pointer', border:'none',
                            background: g.status==='Active' ? C.greenBg : C.redBg,
                            color:      g.status==='Active' ? C.green   : C.red }}>
                            <span style={{ width:20, height:10, borderRadius:5, position:'relative',
                              display:'inline-block', flexShrink:0,
                              background: g.status==='Active' ? '#10b981' : '#e2e8f0' }}>
                              <span style={{ position:'absolute', top:1, width:8, height:8,
                                borderRadius:'50%', background:'#fff',
                                boxShadow:'0 1px 2px rgba(0,0,0,.3)',
                                left: g.status==='Active' ? 10 : 2 }}/>
                            </span>
                            {g.status}
                          </button>
                          {g.last_run_status && <StatusBadge s={g.last_run_status}/>}
                        </div>
                      </td>

                      {/* Row count */}
                      <td style={{ padding:'4px 8px', textAlign:'right' }}>
                        {g.last_run_rows != null
                          ? <strong style={{ fontSize:10, color:C.text }}>{g.last_run_rows.toLocaleString()}</strong>
                          : <span style={{ color:C.textMuted }}>—</span>}
                      </td>

                      {/* Duration */}
                      <td style={{ padding:'4px 8px', textAlign:'center' }}>
                        {g.duration_sec != null ? (
                          <span style={{ fontSize:9, color:'#059669', fontWeight:600 }}>{g.duration_sec}s</span>
                        ) : <span style={{ fontSize:9, color:C.textMuted }}>—</span>}
                      </td>

                      {/* Alerts/Warnings */}
                      <td style={{ padding:'4px 8px', textAlign:'center' }}>
                        {g.last_run_error && g.last_run_error.startsWith('⚠') ? (
                          <span title={g.last_run_error} style={{ cursor:'pointer', fontSize:14 }}>⚠️</span>
                        ) : g.last_run_error ? (
                          <span title={g.last_run_error} style={{ cursor:'pointer', fontSize:14 }}>❌</span>
                        ) : g.last_run_status === 'Success' ? (
                          <span title="No issues" style={{ fontSize:14 }}>✅</span>
                        ) : null}
                      </td>

                      {/* Actions — icon buttons only */}
                      <td style={{ padding:'4px 8px' }}>
                        <div style={{ display:'flex', gap:3, alignItems:'center' }}>
                          {g.last_run_rows > 0 && (
                            <button onClick={() => navigate(`/tables/${encodeURIComponent(g.output_table)}?from=grid-builder`)}
                              title="View data" style={{ padding:3, borderRadius:4, border:'none', cursor:'pointer', background:C.blueBg, color:C.blue, display:'flex' }}>
                              <Eye size={11}/>
                            </button>
                          )}
                          <button onClick={() => handleRun(g)} disabled={isRunning || runningAll}
                            title="Run" style={{ padding:3, borderRadius:4, border:'none', cursor: isRunning?'not-allowed':'pointer', background:C.greenBg, color:C.green, display:'flex', opacity:(isRunning||runningAll)?.5:1 }}>
                            {isRunning ? <Loader size={11} style={{ animation:'spin 1s linear infinite' }}/> : <Play size={11}/>}
                          </button>
                          <button onClick={() => { setEditing(g); setModalOpen(true) }}
                            title="Edit" style={{ padding:3, borderRadius:4, border:'none', cursor:'pointer', background:C.primaryLt, color:C.primary, display:'flex' }}>
                            <Edit3 size={11}/>
                          </button>
                          {deleteConf === g.id ? (
                            <div style={{ display:'flex', gap:2 }}>
                              <button onClick={() => handleDelete(g.id)}
                                style={{ padding:'2px 6px', borderRadius:4, fontSize:9, fontWeight:700, cursor:'pointer', border:'none', background:C.red, color:'#fff' }}>
                                Yes
                              </button>
                              <button onClick={() => setDeleteConf(null)}
                                style={{ padding:'2px 4px', borderRadius:4, fontSize:9, cursor:'pointer', border:'none', background:C.grayBg, color:C.textSub }}>
                                <X size={9}/>
                              </button>
                            </div>
                          ) : (
                            <button onClick={() => setDeleteConf(g.id)}
                              title="Delete" style={{ padding:3, borderRadius:4, border:'none', cursor:'pointer', background:C.redBg, color:C.red, display:'flex' }}>
                              <Trash2 size={11}/>
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Footer */}
        {grids.length > 0 && (
          <div style={{ padding:'9px 18px', borderTop:`1px solid ${C.cardBorder}`,
            background:C.headerBg, fontSize:12, color:C.textMuted }}>
            Each grid: <strong style={{color:C.textSub}}>CREATE TABLE IF NOT EXISTS</strong> →{' '}
            <strong style={{color:C.textSub}}>TRUNCATE</strong> →{' '}
            <strong style={{color:C.textSub}}>INSERT</strong> on every run.
            Active SLOCs from <code style={{fontSize:11}}>ARS_STORE_SLOC_SETTINGS</code>.
          </div>
        )}
      </div>

      {/* Modals */}
      <GridModal open={modalOpen} onClose={() => { setModalOpen(false); setEditing(null) }}
        onSave={handleSave} availableCols={availCols} editing={editing} allGrids={grids}/>
      <RunResultsModal results={runResults} onClose={() => setRunResults(null)}/>

      {/* Calculation Log Modal */}
      {calcLog && (
        <div style={{ position:'fixed', inset:0, background:'rgba(0,0,0,.5)', display:'flex',
          alignItems:'center', justifyContent:'center', zIndex:1000 }}
          onClick={() => setCalcLog(null)}>
          <div onClick={e => e.stopPropagation()} style={{
            background:'#fff', borderRadius:8, width:600, maxHeight:'80vh', overflow:'hidden',
            boxShadow:'0 20px 60px rgba(0,0,0,.2)', display:'flex', flexDirection:'column' }}>
            <div style={{ padding:'10px 14px', background:'#f8fafc', borderBottom:'1px solid #e2e8f0',
              display:'flex', justifyContent:'space-between', alignItems:'center' }}>
              <span style={{ fontSize:12, fontWeight:700 }}>
                Pre-Grid Calculation
                {calcLog?.duration > 0 && <span style={{ fontSize:9, fontWeight:400, color:'#059669', marginLeft:8 }}>⏱ {calcLog.duration}s</span>}
              </span>
              <button onClick={() => setCalcLog(null)} style={{ background:'none', border:'none', cursor:'pointer' }}>
                <X size={14}/>
              </button>
            </div>
            <div style={{ overflow:'auto', flex:1, padding:10 }}>
              <table style={{ width:'100%', borderCollapse:'collapse', fontSize:10 }}>
                <thead>
                  <tr style={{ background:'#f1f5f9', borderBottom:'2px solid #e2e8f0' }}>
                    <th style={{ padding:'5px 8px', textAlign:'left', fontSize:9, fontWeight:700 }}>#</th>
                    <th style={{ padding:'5px 8px', textAlign:'left', fontSize:9, fontWeight:700 }}>Step</th>
                    <th style={{ padding:'5px 8px', textAlign:'left', fontSize:9, fontWeight:700 }}>Detail</th>
                    <th style={{ padding:'5px 8px', textAlign:'center', fontSize:9, fontWeight:700 }}>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {(calcLog?.steps || []).map((s, i) => (
                    <tr key={i} style={{ borderBottom:'1px solid #f1f5f9' }}>
                      <td style={{ padding:'4px 8px', color:'#94a3b8' }}>{i + 1}</td>
                      <td style={{ padding:'4px 8px', fontWeight:600 }}>{s.step}</td>
                      <td style={{ padding:'4px 8px', color:'#475569', maxWidth:300, wordBreak:'break-word' }}>{s.detail}</td>
                      <td style={{ padding:'4px 8px', textAlign:'center' }}>
                        {s.status === 'ok' ? '✅' : s.status === 'skip' ? '⏭️' : '❌'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(calcLog?.steps || []).length === 0 && (
                <div style={{ padding:20, textAlign:'center', color:'#94a3b8', fontSize:11 }}>No calculation steps recorded</div>
              )}
            </div>
          </div>
        </div>
      )}

      <style>{`
        @keyframes spin{to{transform:rotate(360deg);}}
        input.sec-cap-pct::-webkit-outer-spin-button,
        input.sec-cap-pct::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }
      `}</style>
    </div>
  )
}
