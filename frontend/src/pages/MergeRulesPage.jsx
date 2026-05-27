/**
 * MergeRulesPage — CRUD on ARS_MERGE_RULES.
 * Drives MERGE_<col> hierarchy resolution and Master_CONT_MERGE_<col> derivation.
 * Style matches GridBuilderPage (light theme, C palette).
 */
import { useState, useEffect, useMemo, useRef } from 'react'
import { mergeRulesAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Plus, RefreshCw, Trash2, Edit3, X, Save, CheckCircle2, XCircle,
  AlertTriangle, GitMerge, Upload, Download, ChevronDown, ChevronRight,
  FileText,
} from 'lucide-react'
import { C } from '@/theme/colors'

const AGG_OPTIONS = ['SUM', 'AVG', 'MAX', 'MIN']

/* ── tiny helpers (local copies — keeps coupling low) ─────────────────────── */
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

const Select = ({ value, onChange, options, ...rest }) => (
  <select value={value} onChange={onChange} {...rest}
    style={{ padding:'7px 11px', borderRadius:7, fontSize:13,
      background:C.inputBg, border:`1px solid ${C.inputBd}`,
      color:C.text, outline:'none', fontFamily:'inherit', ...rest.style }}>
    {options.map(o => <option key={o} value={o}>{o}</option>)}
  </select>
)

/* ── Modal: Add / Edit ────────────────────────────────────────────────────── */
const EMPTY = { source_col:'', source_value:'', target_value:'', agg:'SUM', active:true }

const RuleModal = ({ open, onClose, onSave, editing, sourceCols }) => {
  const [form, setForm] = useState(EMPTY)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (editing) setForm({
      source_col:   editing.source_col,
      source_value: editing.source_value,
      target_value: editing.target_value,
      agg:          editing.agg || 'SUM',
      active:       !!editing.active,
    })
    else setForm(EMPTY)
  }, [editing, open])

  if (!open) return null
  const set = (k, v) => setForm(p => ({ ...p, [k]: v }))

  const handleSave = async () => {
    if (!form.source_col.trim()) { toast.error('source_col is required'); return }
    if (form.source_col.toUpperCase().startsWith('MERGE_')) {
      toast.error('source_col must NOT start with MERGE_ — that is the derived side')
      return
    }
    if (!form.source_value.trim() || !form.target_value.trim()) {
      toast.error('source_value and target_value are required'); return
    }
    setSaving(true)
    try { await onSave(form) } finally { setSaving(false) }
  }

  return (
    <div style={{ position:'fixed', inset:0, background:'rgba(0,0,0,.5)',
      display:'flex', alignItems:'center', justifyContent:'center', zIndex:1000 }}>
      <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:14,
        width:'min(540px, 95vw)', maxHeight:'90vh', overflow:'auto',
        boxShadow:'0 20px 60px rgba(0,0,0,.2)' }}>

        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
          padding:'16px 20px', borderBottom:`1px solid ${C.cardBorder}`, background:C.headerBg }}>
          <h2 style={{ margin:0, fontSize:16, fontWeight:700, color:C.text }}>
            {editing ? `Edit rule #${editing.rule_id}` : 'New merge rule'}
          </h2>
          <button onClick={onClose} style={{ border:'none', background:'none', cursor:'pointer', color:C.textSub }}>
            <X size={18}/>
          </button>
        </div>

        <div style={{ padding:'18px 20px', display:'flex', flexDirection:'column', gap:14 }}>
          <Field label="Source column (parent dimension)" required>
            <Input
              value={form.source_col}
              onChange={(e) => set('source_col', e.target.value.toUpperCase())}
              placeholder="e.g. RNG_SEG"
              disabled={!!editing}
              list="src-cols-suggest"
              style={editing ? { background:C.grayBg, color:C.textMuted } : {}}
            />
            <datalist id="src-cols-suggest">
              {sourceCols.map(c => <option key={c} value={c} />)}
            </datalist>
          </Field>

          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:12 }}>
            <Field label="Source value" required>
              <Input
                value={form.source_value}
                onChange={(e) => set('source_value', e.target.value)}
                placeholder="e.g. E"
                disabled={!!editing}
                style={editing ? { background:C.grayBg, color:C.textMuted } : {}}
              />
            </Field>
            <Field label="Target value (merged bucket)" required>
              <Input
                value={form.target_value}
                onChange={(e) => set('target_value', e.target.value)}
                placeholder="e.g. EV"
              />
            </Field>
          </div>

          <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:12, alignItems:'end' }}>
            <Field label="Aggregation">
              <Select
                value={form.agg}
                onChange={(e) => set('agg', e.target.value)}
                options={AGG_OPTIONS}
              />
            </Field>
            <Field label="Active">
              <label style={{ display:'flex', alignItems:'center', gap:8, fontSize:13, color:C.text, height:'34px' }}>
                <input
                  type="checkbox"
                  checked={form.active}
                  onChange={(e) => set('active', e.target.checked)}
                  style={{ width:16, height:16 }}
                />
                Participates in derivation
              </label>
            </Field>
          </div>

          {!editing && (
            <div style={{ padding:'10px 12px', background:C.blueBg, border:`1px solid ${C.blueBd}`,
              borderRadius:8, fontSize:12, color:C.blue, display:'flex', alignItems:'flex-start', gap:8 }}>
              <AlertTriangle size={14} style={{ marginTop:1, flexShrink:0 }} />
              <span>
                On save, <strong>Master_CONT_MERGE_{form.source_col || '<col>'}</strong> is
                automatically rebuilt from <strong>Master_CONT_{form.source_col || '<col>'}</strong>.
              </span>
            </div>
          )}
        </div>

        <div style={{ display:'flex', justifyContent:'flex-end', gap:8,
          padding:'14px 20px', borderTop:`1px solid ${C.cardBorder}`, background:C.headerBg }}>
          <Btn color="gray" onClick={onClose}>Cancel</Btn>
          <Btn onClick={handleSave} disabled={saving}>
            <Save size={14}/> {saving ? 'Saving…' : (editing ? 'Update' : 'Create')}
          </Btn>
        </div>
      </div>
    </div>
  )
}

/* ── CSV helpers (tiny parser; no embedded commas/quotes — fine for codes) ── */
const CSV_HEADERS = ['source_col', 'source_value', 'target_value', 'agg', 'active']

const TEMPLATE_CSV = [
  CSV_HEADERS.join(','),
  'RNG_SEG,E,EV,SUM,true',
  'RNG_SEG,V,EV,SUM,true',
  'RNG_SEG,P,PSP,SUM,true',
  'RNG_SEG,SP,PSP,SUM,true',
  '# Add more rows below. agg = SUM|AVG|MAX|MIN. active = true|false. Lines starting with # are ignored.',
].join('\n')

const downloadTemplate = () => {
  const blob = new Blob([TEMPLATE_CSV], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'merge_rules_template.csv'
  document.body.appendChild(a); a.click(); document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

const parseCsv = (text) => {
  const lines = text.split(/\r?\n/)
    .map(l => l.trim())
    .filter(l => l.length > 0 && !l.startsWith('#'))
  if (lines.length === 0) return { rules: [], errors: [{ row: 0, error: 'file is empty' }] }

  // Header row — case-insensitive, allow ordering flex
  const headers = lines[0].split(',').map(h => h.trim().toLowerCase())
  const missing = CSV_HEADERS.filter(h => !headers.includes(h))
  if (missing.length) {
    return {
      rules: [],
      errors: [{ row: 1, error: `missing header column(s): ${missing.join(', ')}. Expected: ${CSV_HEADERS.join(', ')}` }],
    }
  }
  const idx = {}
  CSV_HEADERS.forEach(h => { idx[h] = headers.indexOf(h) })

  const rules = []
  const errors = []
  for (let i = 1; i < lines.length; i++) {
    const cells = lines[i].split(',').map(c => c.trim())
    const row = {
      source_col:   cells[idx.source_col]   || '',
      source_value: cells[idx.source_value] || '',
      target_value: cells[idx.target_value] || '',
      agg:          (cells[idx.agg] || 'SUM').toUpperCase(),
      active:       /^(true|1|yes|y)$/i.test((cells[idx.active] || 'true').trim()),
    }
    if (!row.source_col || !row.source_value || !row.target_value) {
      errors.push({ row: i + 1, error: `missing required value(s) in: ${lines[i]}` })
      continue
    }
    if (row.source_col.toUpperCase().startsWith('MERGE_')) {
      errors.push({ row: i + 1, error: `source_col must NOT start with MERGE_ (got ${row.source_col})` })
      continue
    }
    if (!['SUM', 'AVG', 'MAX', 'MIN'].includes(row.agg)) {
      errors.push({ row: i + 1, error: `invalid agg '${row.agg}' — use SUM/AVG/MAX/MIN` })
      continue
    }
    row.source_col = row.source_col.toUpperCase()
    rules.push(row)
  }
  return { rules, errors }
}

/* ── Modal: Bulk upload ───────────────────────────────────────────────────── */
const BulkModal = ({ open, onClose, onSubmit }) => {
  const [file, setFile] = useState(null)
  const [parsed, setParsed] = useState({ rules: [], errors: [] })
  const [submitting, setSubmitting] = useState(false)
  const fileRef = useRef(null)

  useEffect(() => { if (!open) { setFile(null); setParsed({ rules: [], errors: [] }); setSubmitting(false) } }, [open])

  const handleFile = async (f) => {
    if (!f) return
    setFile(f)
    const text = await f.text()
    setParsed(parseCsv(text))
  }

  if (!open) return null
  const canSubmit = parsed.rules.length > 0 && parsed.errors.length === 0 && !submitting

  return (
    <div style={{ position:'fixed', inset:0, background:'rgba(0,0,0,.5)',
      display:'flex', alignItems:'center', justifyContent:'center', zIndex:1000 }}>
      <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:14,
        width:'min(720px, 95vw)', maxHeight:'90vh', overflow:'auto',
        boxShadow:'0 20px 60px rgba(0,0,0,.2)' }}>

        <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center',
          padding:'16px 20px', borderBottom:`1px solid ${C.cardBorder}`, background:C.headerBg }}>
          <h2 style={{ margin:0, fontSize:16, fontWeight:700, color:C.text, display:'flex', alignItems:'center', gap:8 }}>
            <Upload size={16}/> Bulk upload merge rules
          </h2>
          <button onClick={onClose} style={{ border:'none', background:'none', cursor:'pointer', color:C.textSub }}>
            <X size={18}/>
          </button>
        </div>

        <div style={{ padding:'18px 20px', display:'flex', flexDirection:'column', gap:14 }}>
          {/* Template download row */}
          <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between',
            padding:'10px 12px', background:C.blueBg, border:`1px solid ${C.blueBd}`,
            borderRadius:8, fontSize:12, color:C.blue }}>
            <div style={{ display:'flex', alignItems:'center', gap:8 }}>
              <FileText size={14}/>
              <span>Columns: <code>{CSV_HEADERS.join(', ')}</code>. Lines starting with <code>#</code> are ignored.</span>
            </div>
            <Btn color="blue" onClick={downloadTemplate} style={{ padding:'5px 10px' }}>
              <Download size={13}/> Template
            </Btn>
          </div>

          {/* File picker */}
          <div>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => handleFile(e.target.files?.[0])}
              style={{ display:'none' }}
            />
            <div style={{ border:`2px dashed ${C.cardBorder}`, borderRadius:10,
              padding:'24px 18px', textAlign:'center', cursor:'pointer',
              background: file ? C.greenBg : '#fafbfc' }}
              onClick={() => fileRef.current?.click()}>
              <Upload size={24} color={file ? C.green : C.textMuted} style={{ marginBottom:6 }}/>
              <div style={{ fontSize:13, fontWeight:600, color: file ? C.green : C.text }}>
                {file ? file.name : 'Click to pick a CSV file'}
              </div>
              {file && (
                <div style={{ fontSize:11, color:C.textSub, marginTop:4 }}>
                  {(file.size / 1024).toFixed(1)} KB — {parsed.rules.length} valid rule(s)
                  {parsed.errors.length > 0 && <span style={{ color:C.red }}> · {parsed.errors.length} error(s)</span>}
                </div>
              )}
            </div>
          </div>

          {/* Validation errors */}
          {parsed.errors.length > 0 && (
            <div style={{ padding:'10px 12px', background:C.redBg, border:`1px solid ${C.redBd}`,
              borderRadius:8, fontSize:12, color:C.red, maxHeight:140, overflow:'auto' }}>
              <div style={{ fontWeight:600, marginBottom:6, display:'flex', alignItems:'center', gap:6 }}>
                <AlertTriangle size={13}/> Fix these before uploading:
              </div>
              <ul style={{ margin:0, paddingLeft:18, lineHeight:1.6 }}>
                {parsed.errors.slice(0, 20).map((e, i) => (
                  <li key={i}>Row {e.row}: {e.error}</li>
                ))}
                {parsed.errors.length > 20 && <li>… and {parsed.errors.length - 20} more</li>}
              </ul>
            </div>
          )}

          {/* Preview */}
          {parsed.rules.length > 0 && parsed.errors.length === 0 && (
            <div>
              <div style={{ fontSize:12, fontWeight:600, color:C.textSub, marginBottom:6 }}>
                Preview ({parsed.rules.length} row{parsed.rules.length === 1 ? '' : 's'})
              </div>
              <div style={{ maxHeight:240, overflow:'auto', border:`1px solid ${C.cardBorder}`, borderRadius:8 }}>
                <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12 }}>
                  <thead style={{ position:'sticky', top:0, background:C.headerBg }}>
                    <tr>
                      {CSV_HEADERS.map(h => (
                        <th key={h} style={{ textAlign:'left', padding:'6px 10px', fontSize:11,
                          fontWeight:600, color:C.textSub, borderBottom:`1px solid ${C.cardBorder}` }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {parsed.rules.slice(0, 50).map((r, i) => (
                      <tr key={i} style={{ borderBottom:`1px solid ${C.cardBorder}` }}>
                        <td style={{ padding:'5px 10px', fontFamily:'monospace' }}>{r.source_col}</td>
                        <td style={{ padding:'5px 10px', fontFamily:'monospace' }}>{r.source_value}</td>
                        <td style={{ padding:'5px 10px', fontFamily:'monospace', color:C.primary }}>{r.target_value}</td>
                        <td style={{ padding:'5px 10px' }}>{r.agg}</td>
                        <td style={{ padding:'5px 10px' }}>{r.active ? 'true' : 'false'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {parsed.rules.length > 50 && (
                  <div style={{ padding:'6px 10px', fontSize:11, color:C.textMuted, background:C.headerBg }}>
                    … {parsed.rules.length - 50} more row(s) not shown
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <div style={{ display:'flex', justifyContent:'flex-end', gap:8,
          padding:'14px 20px', borderTop:`1px solid ${C.cardBorder}`, background:C.headerBg }}>
          <Btn color="gray" onClick={onClose}>Cancel</Btn>
          <Btn onClick={async () => {
                  setSubmitting(true)
                  try { await onSubmit(parsed.rules) } finally { setSubmitting(false) }
                }}
               disabled={!canSubmit}>
            <Save size={14}/> {submitting ? 'Uploading…' : `Upload ${parsed.rules.length} rule(s)`}
          </Btn>
        </div>
      </div>
    </div>
  )
}

/* ── Refresh result toast body ────────────────────────────────────────────── */
const formatRefresh = (info) => {
  if (!info) return null
  if (info.status === 'skipped') return `Skipped: ${info.reason}`
  if (info.status === 'error')   return `Error: ${info.reason}`
  const drift = Math.abs((info.parent_total ?? 0) - (info.derived_total ?? 0))
  const driftStr = drift > 0.01 ? `, DRIFT ${drift.toFixed(4)}` : ''
  return `Derived: ${info.rows} rows from ${info.parent_rows} parent rows (agg=${info.agg}${driftStr})`
}

/* ── Main page ────────────────────────────────────────────────────────────── */
export default function MergeRulesPage() {
  const [rules, setRules] = useState([])
  const [sourceCols, setSourceCols] = useState([])
  const [loading, setLoading] = useState(true)
  const [modalOpen, setModalOpen] = useState(false)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [refreshing, setRefreshing] = useState({})  // {source_col: bool}
  const [collapsed, setCollapsed] = useState(() => new Set())  // source_cols that are collapsed

  const load = async () => {
    setLoading(true)
    try {
      const [rulesRes, colsRes] = await Promise.all([
        mergeRulesAPI.list(),
        mergeRulesAPI.sourceCols(),
      ])
      setRules(rulesRes.data?.data?.rules || [])
      setSourceCols(colsRes.data?.data?.source_cols || [])
    } catch (e) {
      // toast handled by interceptor
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const onSave = async (form) => {
    try {
      if (editing) {
        const { data } = await mergeRulesAPI.update(editing.rule_id, {
          target_value: form.target_value,
          agg:          form.agg,
          active:       form.active,
        })
        toast.success(`Rule updated. ${formatRefresh(data?.data?.derived_refresh) || ''}`)
      } else {
        const { data } = await mergeRulesAPI.create(form)
        toast.success(`Rule created. ${formatRefresh(data?.data?.derived_refresh) || ''}`)
      }
      setModalOpen(false); setEditing(null)
      await load()
    } catch { /* interceptor toasts */ }
  }

  const onDelete = async (rule) => {
    if (!window.confirm(`Delete rule: ${rule.source_col} '${rule.source_value}' → '${rule.target_value}'?`)) return
    try {
      const { data } = await mergeRulesAPI.remove(rule.rule_id)
      toast.success(`Rule deleted. ${formatRefresh(data?.data?.derived_refresh) || ''}`)
      await load()
    } catch { /* interceptor */ }
  }

  const onRefresh = async (source_col) => {
    setRefreshing(p => ({ ...p, [source_col]: true }))
    try {
      const { data } = await mergeRulesAPI.refresh(source_col)
      const info = data?.data
      const msg = formatRefresh(info)
      if (info?.warning) toast.error(`${source_col}: ${info.warning}`, { duration: 8000 })
      else toast.success(`${source_col} — ${msg}`)
    } catch { /* interceptor */ } finally {
      setRefreshing(p => ({ ...p, [source_col]: false }))
    }
  }

  const onBulkSubmit = async (rules) => {
    try {
      const { data } = await mergeRulesAPI.bulk(rules, true)
      const d = data?.data || {}
      const dims = (d.dimensions || []).join(', ')
      toast.success(
        `Bulk: ${d.inserted} inserted, ${d.updated} updated across [${dims}]`,
        { duration: 6000 }
      )
      // Surface per-dimension drift warnings if any
      Object.entries(d.derived_refresh || {}).forEach(([src, info]) => {
        if (info?.warning) toast.error(`${src}: ${info.warning}`, { duration: 8000 })
      })
      setBulkOpen(false)
      await load()
    } catch (e) {
      // FastAPI returns errors as data.detail = {message, errors:[...]}
      const detail = e.response?.data?.detail
      if (detail && typeof detail === 'object' && detail.errors) {
        const lines = detail.errors.slice(0, 5).map(x => `Row ${x.row}: ${x.error}`).join(' · ')
        toast.error(`${detail.message}: ${lines}`, { duration: 8000 })
      }
      // else: interceptor toasts the generic error
    }
  }

  const toggleCollapsed = (source_col) => {
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(source_col)) next.delete(source_col)
      else next.add(source_col)
      return next
    })
  }

  const expandAll = () => setCollapsed(new Set())
  const collapseAll = () => setCollapsed(new Set(rules.map(r => r.source_col)))

  // Group rules by source_col for the grouped table
  const grouped = useMemo(() => {
    const m = new Map()
    for (const r of rules) {
      if (!m.has(r.source_col)) m.set(r.source_col, [])
      m.get(r.source_col).push(r)
    }
    return Array.from(m.entries()).sort((a, b) => a[0].localeCompare(b[0]))
  }, [rules])

  return (
    <div style={{ padding:'18px 22px', background:'#f5f7fa', minHeight:'100vh' }}>
      {/* Header */}
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:16 }}>
        <div>
          <div style={{ display:'flex', alignItems:'center', gap:9, marginBottom:2 }}>
            <GitMerge size={20} color={C.primary} />
            <h1 style={{ margin:0, fontSize:18, fontWeight:700, color:C.text }}>Merge Rules</h1>
          </div>
          <div style={{ fontSize:12, color:C.textSub }}>
            Drives <code>MERGE_&lt;col&gt;</code> hierarchy resolution and
            auto-derives <code>Master_CONT_MERGE_&lt;col&gt;</code> from its parent.
          </div>
        </div>
        <div style={{ display:'flex', gap:8 }}>
          {grouped.length > 1 && (
            <>
              <Btn color="gray" onClick={collapseAll} style={{ padding:'7px 10px' }}>
                <ChevronRight size={13}/> Collapse all
              </Btn>
              <Btn color="gray" onClick={expandAll} style={{ padding:'7px 10px' }}>
                <ChevronDown size={13}/> Expand all
              </Btn>
            </>
          )}
          <Btn color="gray" onClick={load} disabled={loading}>
            <RefreshCw size={14} style={{ animation: loading ? 'spin 1s linear infinite' : 'none' }} /> Reload
          </Btn>
          <Btn color="blue" onClick={() => setBulkOpen(true)}>
            <Upload size={14}/> Bulk upload
          </Btn>
          <Btn onClick={() => { setEditing(null); setModalOpen(true) }}>
            <Plus size={14}/> New rule
          </Btn>
        </div>
      </div>

      {/* Empty state */}
      {!loading && rules.length === 0 && (
        <div style={{ background:C.card, border:`1px solid ${C.cardBorder}`, borderRadius:12,
          padding:'28px 22px', textAlign:'center' }}>
          <GitMerge size={36} color={C.textMuted} style={{ marginBottom:8 }} />
          <h3 style={{ margin:0, fontSize:14, fontWeight:600, color:C.text }}>No merge rules yet</h3>
          <p style={{ margin:'6px 0 16px', fontSize:12, color:C.textSub }}>
            Create rules to map source values (E, V, P, SP) into merged buckets (EV, PSP).<br/>
            Each rule is one row: one source value → one target bucket.
          </p>
          <Btn onClick={() => { setEditing(null); setModalOpen(true) }}>
            <Plus size={14}/> Create first rule
          </Btn>
        </div>
      )}

      {/* Grouped tables (one block per source_col) */}
      {grouped.map(([source_col, rows]) => {
        const activeRows = rows.filter(r => r.active)
        const targets = Array.from(new Set(activeRows.map(r => r.target_value))).sort()
        const aggs = Array.from(new Set(activeRows.map(r => r.agg)))
        const aggConflict = aggs.length > 1
        const isCollapsed = collapsed.has(source_col)
        return (
          <div key={source_col} style={{ background:C.card, border:`1px solid ${C.cardBorder}`,
            borderRadius:12, marginBottom:14, overflow:'hidden' }}>
            {/* Section header (clickable to toggle) */}
            <div style={{ padding:'12px 16px', background:C.headerBg,
              borderBottom: isCollapsed ? 'none' : `1px solid ${C.cardBorder}`,
              display:'flex', justifyContent:'space-between', alignItems:'center', gap:12, flexWrap:'wrap' }}>
              <div
                onClick={() => toggleCollapsed(source_col)}
                style={{ cursor:'pointer', flex:1, display:'flex', alignItems:'center', gap:8 }}
                title={isCollapsed ? 'Expand' : 'Collapse'}>
                {isCollapsed
                  ? <ChevronRight size={16} color={C.textSub}/>
                  : <ChevronDown size={16} color={C.textSub}/>}
                <div>
                  <div style={{ display:'flex', alignItems:'center', gap:8 }}>
                    <span style={{ fontSize:14, fontWeight:700, color:C.text, fontFamily:'monospace' }}>
                      {source_col}
                    </span>
                    <span style={{ fontSize:11, color:C.textMuted }}>→</span>
                    <span style={{ fontSize:13, fontWeight:600, color:C.primary, fontFamily:'monospace' }}>
                      MERGE_{source_col}
                    </span>
                  </div>
                  <div style={{ fontSize:11, color:C.textSub, marginTop:3 }}>
                    {activeRows.length}/{rows.length} active · buckets: {targets.length ? targets.join(', ') : '—'} · agg: {aggs.join('/') || '—'}
                    {aggConflict && (
                      <span style={{ color:C.red, fontWeight:600, marginLeft:8 }}>
                        <AlertTriangle size={11} style={{ verticalAlign:'-1px' }}/> agg conflict
                      </span>
                    )}
                  </div>
                </div>
              </div>
              <Btn color="blue"
                onClick={(e) => { e.stopPropagation(); onRefresh(source_col) }}
                disabled={!!refreshing[source_col]}>
                <RefreshCw size={13}
                  style={{ animation: refreshing[source_col] ? 'spin 1s linear infinite' : 'none' }}/>
                Refresh derived
              </Btn>
            </div>

            {/* Rules table (hidden when collapsed) */}
            {!isCollapsed && (
            <table style={{ width:'100%', borderCollapse:'collapse', fontSize:12 }}>
              <thead>
                <tr style={{ background:'#fafbfc', borderBottom:`1px solid ${C.cardBorder}` }}>
                  <th style={th}>#</th>
                  <th style={th}>Source value</th>
                  <th style={th}>Target value</th>
                  <th style={th}>Agg</th>
                  <th style={th}>Active</th>
                  <th style={th}>Modified</th>
                  <th style={{ ...th, textAlign:'right' }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.rule_id} style={{ borderBottom:`1px solid ${C.cardBorder}` }}>
                    <td style={td}>{r.rule_id}</td>
                    <td style={{ ...td, fontFamily:'monospace', fontWeight:600 }}>{r.source_value}</td>
                    <td style={{ ...td, fontFamily:'monospace', color:C.primary, fontWeight:600 }}>{r.target_value}</td>
                    <td style={td}>{r.agg}</td>
                    <td style={td}>
                      {r.active
                        ? <CheckCircle2 size={14} color={C.green} />
                        : <XCircle size={14} color={C.textMuted} />}
                    </td>
                    <td style={{ ...td, fontSize:11, color:C.textSub }}>
                      {r.modified_at ? new Date(r.modified_at).toLocaleString() : '—'}
                      {r.modified_by && <span style={{ display:'block', color:C.textMuted }}>by {r.modified_by}</span>}
                    </td>
                    <td style={{ ...td, textAlign:'right' }}>
                      <div style={{ display:'inline-flex', gap:6 }}>
                        <button onClick={() => { setEditing(r); setModalOpen(true) }}
                          style={iconBtn} title="Edit">
                          <Edit3 size={13}/>
                        </button>
                        <button onClick={() => onDelete(r)}
                          style={{ ...iconBtn, color:C.red }} title="Delete">
                          <Trash2 size={13}/>
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            )}
          </div>
        )
      })}

      {/* Help footer */}
      {!loading && rules.length > 0 && (
        <div style={{ marginTop:14, padding:'10px 14px', background:C.card,
          border:`1px solid ${C.cardBorder}`, borderRadius:10, fontSize:11, color:C.textSub, lineHeight:1.6 }}>
          <strong style={{ color:C.text }}>How this works.</strong>{' '}
          Uploading <code>Master_CONT_&lt;col&gt;</code> auto-refreshes the derived
          <code> Master_CONT_MERGE_&lt;col&gt;</code>. Grids whose hierarchy includes
          a <code>MERGE_&lt;col&gt;</code> column resolve via these rules at pivot time.
          <em> Direct SQL edits to <code>ARS_MERGE_RULES</code> do NOT auto-refresh — use “Refresh derived” afterwards.</em>
        </div>
      )}

      <RuleModal
        open={modalOpen}
        onClose={() => { setModalOpen(false); setEditing(null) }}
        onSave={onSave}
        editing={editing}
        sourceCols={sourceCols}
      />

      <BulkModal
        open={bulkOpen}
        onClose={() => setBulkOpen(false)}
        onSubmit={onBulkSubmit}
      />
    </div>
  )
}

const th = { textAlign:'left', padding:'8px 12px', fontSize:11, fontWeight:600, color:C.textSub, textTransform:'uppercase', letterSpacing:'.04em' }
const td = { padding:'8px 12px', color:C.text, verticalAlign:'middle' }
const iconBtn = { border:'none', background:'transparent', cursor:'pointer', color:C.textSub, padding:4, borderRadius:4 }
