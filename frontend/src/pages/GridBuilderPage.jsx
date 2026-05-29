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
const EMPTY_FORM = { grid_name:'', description:'', hierarchy_columns:[], kpi_filter:'', output_table:'', status:'Active', pivot_only:false, weightage:1.0, grid_group:'Primary', use_for_opt_sale:false }

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
      use_for_opt_sale: !!editing.use_for_opt_sale,
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

  /* run all */
  const handleRunAll = async () => {
    setRunningAll(true)
    setGrids(prev => prev.map(g => g.status === 'Active' ? { ...g, last_run_status: 'Running' } : g))
    const stopPoll = pollGrids()
    try {
      const { data } = await gridBuilderAPI.runAll()
      toast.success(data.message)
      setRunResults(data.data.results || [])
      await load()
    } catch {} finally { stopPoll(); setRunningAll(false) }
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
                  {['#','Grid Name','Output Table','Hierarchy','KPI','Group','Wt',
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

                      {/* Hierarchy cols */}
                      <td style={{ padding:'4px 8px' }}>
                        <div style={{ display:'flex', flexWrap:'wrap', gap:2 }}>
                          {(g.hierarchy_columns.length ? g.hierarchy_columns : ['MATNR','WERKS']).map(c => (
                            <span key={c} style={{ fontSize:8, fontWeight:600, color:C.textSub,
                              background:C.grayBg, border:`1px solid ${C.grayBd}`,
                              padding:'0px 4px', borderRadius:3, fontFamily:'monospace' }}>{c}</span>
                          ))}
                        </div>
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

      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
