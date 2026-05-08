/**
 * TrendAdminPage — Create tables & maintenance for Trend_* tables
 */
import { useState, useEffect, useMemo, useRef } from 'react'
import { trendsAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  TrendingUp, Upload, Settings, Plus, Trash2,
  FileSpreadsheet, ChevronDown, ChevronRight, AlertTriangle,
  Database, Columns, Loader2, Edit3
} from 'lucide-react'
import { C } from '@/theme/colors'
const sInput = { height:28, fontSize:11, padding:'0 8px', borderRadius:5, border:`1px solid ${C.inputBorder}`, outline:'none', color:C.text, background:'#fff' }
const sSelect = { ...sInput, paddingRight:20, cursor:'pointer' }
const sBtn = (bg,color,bd) => ({ display:'inline-flex', alignItems:'center', gap:4, height:28, padding:'0 10px', fontSize:11, fontWeight:600, borderRadius:5, border:`1px solid ${bd||bg}`, background:bg, color, cursor:'pointer', whiteSpace:'nowrap' })
const sCard = { background:C.cardBg, border:`1px solid ${C.cardBorder}`, borderRadius:8, overflow:'hidden' }
const sSection = { padding:'10px 14px' }
const sLabel = { fontSize:10, fontWeight:600, color:C.textSub, marginBottom:3, display:'block' }

const SQL_TYPES = [
  'SMALLINT','INT','BIGINT','FLOAT','BIT','DECIMAL(18,2)',
  'DATE','DATETIME2',
  'NVARCHAR(50)','NVARCHAR(100)','NVARCHAR(255)','NVARCHAR(4000)','NVARCHAR(MAX)',
]

function ConfirmModal({ open, title, message, onConfirm, onCancel, danger }) {
  if (!open) return null
  return (
    <div style={{ position:'fixed',inset:0,zIndex:9999,display:'flex',alignItems:'center',justifyContent:'center', background:'rgba(0,0,0,.35)' }}>
      <div style={{ ...sCard, width:380, padding:20 }}>
        <div style={{ fontSize:13, fontWeight:700, color:danger?C.red:C.text, marginBottom:8, display:'flex', alignItems:'center', gap:6 }}>
          {danger && <AlertTriangle size={14}/>} {title}
        </div>
        <div style={{ fontSize:11, color:C.textSub, marginBottom:16, lineHeight:1.5 }}>{message}</div>
        <div style={{ display:'flex', justifyContent:'flex-end', gap:6 }}>
          <button onClick={onCancel} style={sBtn('#fff',C.textSub,C.inputBorder)}>Cancel</button>
          <button onClick={onConfirm} style={sBtn(danger?C.red:C.primary,'#fff')}>
            {danger ? 'Yes, proceed' : 'Confirm'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ── Create new table ──────────────────────────────────────────────────────── */
function CreateTableSection({ open, toggle, onCreated }) {
  const [tableSuffix, setTableSuffix] = useState('')
  const [file, setFile] = useState(null)
  const [columns, setColumns] = useState([])
  const [creating, setCreating] = useState(false)
  const fileRef = useRef()

  const onFile = async (f) => {
    if (!f) return
    setFile(f)
    const fd = new FormData(); fd.append('file', f)
    try {
      const r = await trendsAPI.uploadPreview(fd)
      const data = r.data?.data || r.data
      const cols = (data.columns || []).map(c => ({ name: c.name || c, dtype: c.dtype || c.type || 'NVARCHAR(255)', pk: false }))
      setColumns(cols)
    } catch { toast.error('Preview failed') }
  }

  const setColType = (i, dtype) => setColumns(prev => prev.map((c,j) => j===i ? { ...c, dtype } : c))
  const setColPk = (i, pk) => setColumns(prev => prev.map((c,j) => j===i ? { ...c, pk } : c))

  const doCreate = async (withData) => {
    if (!tableSuffix.trim()) { toast.error('Enter table name suffix'); return }
    if (columns.length === 0) { toast.error('Upload a file to define columns'); return }
    setCreating(true)
    try {
      const fd = new FormData()
      fd.append('table_name', 'Trend_' + tableSuffix.trim())
      fd.append('columns', JSON.stringify(columns.map(c => ({ name:c.name, type:c.dtype, primary_key:c.pk }))))
      if (withData && file) fd.append('file', file)
      fd.append('with_data', withData ? 'true' : 'false')
      await trendsAPI.createTable(fd)
      toast.success(`Table Trend_${tableSuffix.trim()} created${withData ? ' with data' : ''}`)
      setTableSuffix(''); setFile(null); setColumns([])
      onCreated()
    } catch (e) { toast.error(e.response?.data?.detail || 'Create failed') }
    finally { setCreating(false) }
  }

  return (
    <div style={{ ...sCard }}>
      <div onClick={toggle} style={{ ...sSection, background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`, display:'flex', alignItems:'center', gap:6, cursor:'pointer' }}>
        {open ? <ChevronDown size={12}/> : <ChevronRight size={12}/>}
        <Plus size={12} style={{ color:C.primary }}/>
        <span style={{ fontSize:12, fontWeight:700, color:C.text }}>Create New Table</span>
      </div>
      {open && (
        <div style={sSection}>
          <div style={{ display:'flex', alignItems:'center', gap:6, marginBottom:10 }}>
            <span style={{ fontSize:11, color:C.textMuted, fontFamily:'monospace' }}>Trend_</span>
            <input value={tableSuffix} onChange={e=>setTableSuffix(e.target.value)}
              placeholder="table_suffix" style={{ ...sInput, flex:1 }}/>
          </div>
          <div style={{ marginBottom:10 }}>
            <label style={sLabel}>Upload file to infer schema</label>
            <button onClick={()=>fileRef.current?.click()} style={sBtn(C.primaryLight,C.primary,C.primaryBd)}>
              <FileSpreadsheet size={11}/> {file ? file.name : 'Choose file'}
            </button>
            <input ref={fileRef} type="file" accept=".xlsx,.xls" hidden onChange={e=>onFile(e.target.files[0])}/>
          </div>
          {columns.length > 0 && (
            <div style={{ overflowX:'auto', marginBottom:10 }}>
              <table style={{ width:'100%', borderCollapse:'collapse', fontSize:10 }}>
                <thead>
                  <tr style={{ background:C.headerBg }}>
                    <th style={{ padding:'4px 6px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600, width:30 }}>PK</th>
                    <th style={{ padding:'4px 6px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600 }}>Column</th>
                    <th style={{ padding:'4px 6px', textAlign:'left', borderBottom:`1px solid ${C.cardBorder}`, color:C.textSub, fontWeight:600, width:180 }}>Type</th>
                  </tr>
                </thead>
                <tbody>
                  {columns.map((col,i) => (
                    <tr key={i} style={{ background:i%2?C.rowAlt:'#fff' }}>
                      <td style={{ padding:'3px 6px', textAlign:'center' }}>
                        <input type="checkbox" checked={col.pk} onChange={e=>setColPk(i,e.target.checked)} style={{ width:13, height:13, cursor:'pointer' }}/>
                      </td>
                      <td style={{ padding:'3px 6px', fontWeight:500, color:C.text, fontFamily:'monospace' }}>{col.name}</td>
                      <td style={{ padding:'3px 6px' }}>
                        <select value={col.dtype} onChange={e=>setColType(i,e.target.value)}
                          style={{ ...sSelect, width:'100%', height:24, fontSize:10 }}>
                          {SQL_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                        </select>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {columns.length > 0 && (
            <div style={{ display:'flex', gap:8 }}>
              <button onClick={()=>doCreate(false)} disabled={creating}
                style={{ ...sBtn(C.primary,'#fff'), opacity:creating?0.5:1 }}>
                {creating ? <Loader2 size={11} className="animate-spin"/> : <Database size={11}/>}
                Create Table Only
              </button>
              {file && (
                <button onClick={()=>doCreate(true)} disabled={creating}
                  style={{ ...sBtn(C.green,'#fff'), opacity:creating?0.5:1 }}>
                  {creating ? <Loader2 size={11} className="animate-spin"/> : <Upload size={11}/>}
                  Create & Upload Data
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ── Table maintenance ─────────────────────────────────────────────────────── */
function MaintenanceSection({ tables, open, toggle, onChanged }) {
  const [selectedTable, setSelectedTable] = useState('')
  const [schema, setSchema] = useState(null)
  const [confirm, setConfirm] = useState(null)
  const [addColName, setAddColName] = useState('')
  const [addColType, setAddColType] = useState('NVARCHAR(255)')
  const [dropCol, setDropCol] = useState('')
  const [alterCol, setAlterCol] = useState('')
  const [alterType, setAlterType] = useState('NVARCHAR(255)')
  const [renameCol, setRenameCol] = useState('')
  const [renameNew, setRenameNew] = useState('')
  const [opLoading, setOpLoading] = useState(false)

  useEffect(() => {
    if (!selectedTable) { setSchema(null); return }
    trendsAPI.getSchema(selectedTable).then(r => setSchema(r.data?.data || r.data)).catch(() => {})
  }, [selectedTable])

  const colNames = useMemo(() => {
    if (!schema) return []
    const cols = schema.columns || schema || []
    return cols.map(c => typeof c === 'string' ? c : c.column_name || c.name)
  }, [schema])

  const doTruncate = () => {
    setConfirm({ action: async () => { try { await trendsAPI.truncateTable(selectedTable); toast.success(`${selectedTable} truncated`) } catch { toast.error('Truncate failed') } },
      title: 'Truncate Table', message: `This will delete ALL rows in "${selectedTable}". This cannot be undone.`, danger: true })
  }

  const doDrop = () => {
    setConfirm({ action: async () => { try { await trendsAPI.dropTable(selectedTable); toast.success(`${selectedTable} dropped`); setSelectedTable(''); onChanged() } catch { toast.error('Drop failed') } },
      title: 'Drop Table', message: `This will permanently DELETE the table "${selectedTable}" and all its data. This cannot be undone.`, danger: true })
  }

  const handleConfirm = async () => { if (confirm?.action) await confirm.action(); setConfirm(null) }

  const doColumnOp = async (op, data) => {
    if (!selectedTable) return
    setOpLoading(true)
    try {
      await trendsAPI.alterColumns(selectedTable, { operation: op, ...data })
      toast.success(`Column operation "${op}" completed`)
      const r = await trendsAPI.getSchema(selectedTable)
      setSchema(r.data?.data || r.data)
      setAddColName(''); setDropCol(''); setAlterCol(''); setRenameCol(''); setRenameNew('')
    } catch (e) { toast.error(e.response?.data?.detail || 'Column op failed') }
    finally { setOpLoading(false) }
  }

  return (
    <>
      <ConfirmModal open={!!confirm} title={confirm?.title||''} message={confirm?.message||''} danger={confirm?.danger} onConfirm={handleConfirm} onCancel={()=>setConfirm(null)}/>
      <div style={{ ...sCard }}>
        <div onClick={toggle} style={{ ...sSection, background:C.headerBg, borderBottom:`1px solid ${C.cardBorder}`, display:'flex', alignItems:'center', gap:6, cursor:'pointer' }}>
          {open ? <ChevronDown size={12}/> : <ChevronRight size={12}/>}
          <Settings size={12} style={{ color:C.amber }}/>
          <span style={{ fontSize:12, fontWeight:700, color:C.text }}>Table Maintenance</span>
        </div>
        {open && (
          <div style={sSection}>
            <div style={{ marginBottom:12 }}>
              <label style={sLabel}>Select Table</label>
              <select value={selectedTable} onChange={e=>setSelectedTable(e.target.value)}
                style={{ ...sSelect, width:'100%', maxWidth:300 }}>
                <option value="">-- select --</option>
                {tables.map(t => { const name = t.table_name || t; return <option key={name} value={name}>{name}</option> })}
              </select>
            </div>
            {selectedTable && (
              <>
                <div style={{ padding:10, borderRadius:6, marginBottom:14, background:C.redBg, border:`1px solid ${C.redBd}` }}>
                  <div style={{ fontSize:10, fontWeight:700, color:C.red, marginBottom:8, display:'flex', alignItems:'center', gap:4 }}>
                    <AlertTriangle size={11}/> Danger Zone
                  </div>
                  <div style={{ display:'flex', gap:6 }}>
                    <button onClick={doTruncate} style={sBtn('#fff',C.red,C.redBd)}><Trash2 size={10}/> Truncate Table</button>
                    <button onClick={doDrop} style={sBtn(C.red,'#fff')}><Trash2 size={10}/> Drop Table</button>
                  </div>
                </div>

                <div style={{ fontSize:11, fontWeight:700, color:C.text, marginBottom:8, display:'flex', alignItems:'center', gap:4 }}>
                  <Columns size={12}/> Column Operations
                </div>

                {/* Add column */}
                <div style={{ padding:8, borderRadius:5, border:`1px solid ${C.cardBorder}`, marginBottom:8, display:'flex', gap:6, alignItems:'flex-end', flexWrap:'wrap' }}>
                  <div><label style={sLabel}>Add Column</label><input value={addColName} onChange={e=>setAddColName(e.target.value)} placeholder="column_name" style={{ ...sInput, width:140 }}/></div>
                  <div><label style={sLabel}>Type</label><select value={addColType} onChange={e=>setAddColType(e.target.value)} style={{ ...sSelect, width:150 }}>{SQL_TYPES.map(t => <option key={t} value={t}>{t}</option>)}</select></div>
                  <button onClick={()=>doColumnOp('add',{column_name:addColName,column_type:addColType})} disabled={!addColName.trim()||opLoading} style={{ ...sBtn(C.primary,'#fff'), opacity:(!addColName.trim()||opLoading)?0.5:1 }}><Plus size={10}/> Add</button>
                </div>

                {/* Drop column */}
                <div style={{ padding:8, borderRadius:5, border:`1px solid ${C.cardBorder}`, marginBottom:8, display:'flex', gap:6, alignItems:'flex-end', flexWrap:'wrap' }}>
                  <div><label style={sLabel}>Drop Column</label><select value={dropCol} onChange={e=>setDropCol(e.target.value)} style={{ ...sSelect, width:200 }}><option value="">-- select --</option>{colNames.map(c => <option key={c} value={c}>{c}</option>)}</select></div>
                  <button onClick={()=>doColumnOp('drop',{column_name:dropCol})} disabled={!dropCol||opLoading} style={{ ...sBtn(C.red,'#fff'), opacity:(!dropCol||opLoading)?0.5:1 }}><Trash2 size={10}/> Drop</button>
                </div>

                {/* Alter column type */}
                <div style={{ padding:8, borderRadius:5, border:`1px solid ${C.cardBorder}`, marginBottom:8, display:'flex', gap:6, alignItems:'flex-end', flexWrap:'wrap' }}>
                  <div><label style={sLabel}>Alter Column Type</label><select value={alterCol} onChange={e=>setAlterCol(e.target.value)} style={{ ...sSelect, width:160 }}><option value="">-- select column --</option>{colNames.map(c => <option key={c} value={c}>{c}</option>)}</select></div>
                  <div><label style={sLabel}>New Type</label><select value={alterType} onChange={e=>setAlterType(e.target.value)} style={{ ...sSelect, width:150 }}>{SQL_TYPES.map(t => <option key={t} value={t}>{t}</option>)}</select></div>
                  <button onClick={()=>doColumnOp('alter',{column_name:alterCol,column_type:alterType})} disabled={!alterCol||opLoading} style={{ ...sBtn(C.amber,'#fff'), opacity:(!alterCol||opLoading)?0.5:1 }}><Edit3 size={10}/> Alter</button>
                </div>

                {/* Rename column */}
                <div style={{ padding:8, borderRadius:5, border:`1px solid ${C.cardBorder}`, display:'flex', gap:6, alignItems:'flex-end', flexWrap:'wrap' }}>
                  <div><label style={sLabel}>Rename Column</label><select value={renameCol} onChange={e=>setRenameCol(e.target.value)} style={{ ...sSelect, width:160 }}><option value="">-- select column --</option>{colNames.map(c => <option key={c} value={c}>{c}</option>)}</select></div>
                  <div><label style={sLabel}>New Name</label><input value={renameNew} onChange={e=>setRenameNew(e.target.value)} placeholder="new_column_name" style={{ ...sInput, width:160 }}/></div>
                  <button onClick={()=>doColumnOp('rename',{column_name:renameCol,new_name:renameNew})} disabled={!renameCol||!renameNew.trim()||opLoading} style={{ ...sBtn(C.primary,'#fff'), opacity:(!renameCol||!renameNew.trim()||opLoading)?0.5:1 }}><Edit3 size={10}/> Rename</button>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </>
  )
}

/* ── Main page ─────────────────────────────────────────────────────────────── */
export default function TrendAdminPage() {
  const [tables, setTables] = useState([])
  const [createOpen, setCreateOpen] = useState(true)

  const refreshTables = () => {
    trendsAPI.listTables().then(r => { const d = r.data?.data; setTables(d?.tables || (Array.isArray(d) ? d : [])) }).catch(() => {})
  }
  useEffect(refreshTables, [])

  return (
    <div style={{ padding:'0 4px' }}>
      <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:10 }}>
        <Plus size={15} style={{ color:C.primary }}/>
        <span style={{ fontSize:14, fontWeight:700, color:C.text }}>Create New Table</span>
      </div>
      <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
        <CreateTableSection tables={tables} open={createOpen} toggle={()=>setCreateOpen(p=>!p)} onCreated={refreshTables}/>
      </div>
    </div>
  )
}
