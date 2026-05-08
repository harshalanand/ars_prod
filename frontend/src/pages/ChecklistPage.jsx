/**
 * ChecklistPage — compact, grouped layout
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import ReactDOM from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { checklistAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  RefreshCw, Plus, Trash2, Upload, Edit3, Download,
  CheckCircle2, AlertTriangle, XCircle, ClipboardList, Search,
  ChevronUp, ChevronDown, X, Database, Eye, CircleCheck,
  FolderOpen, ChevronRight
} from 'lucide-react'
import { C } from '@/theme/colors'

/* Group colour palette — cycles for different groups */
const GROUP_COLORS = [
  { bg:'#eef2ff', bd:'#c7d2fe', fg:'#4f46e5', bar:'#4f46e5' },  // indigo
  { bg:'#ecfdf5', bd:'#a7f3d0', fg:'#059669', bar:'#059669' },  // green
  { bg:'#fffbeb', bd:'#fde68a', fg:'#d97706', bar:'#d97706' },  // amber
  { bg:'#fef2f2', bd:'#fecaca', fg:'#dc2626', bar:'#dc2626' },  // red
  { bg:'#f0f9ff', bd:'#bae6fd', fg:'#0284c7', bar:'#0284c7' },  // sky
  { bg:'#fdf4ff', bd:'#f0abfc', fg:'#a855f7', bar:'#a855f7' },  // purple
  { bg:'#fff7ed', bd:'#fed7aa', fg:'#ea580c', bar:'#ea580c' },  // orange
  { bg:'#f0fdf4', bd:'#bbf7d0', fg:'#16a34a', bar:'#16a34a' },  // emerald
]

function freshness(iso) {
  if (!iso) return { label:'Not checked', color:C.textMuted, bg:'#f1f5f9', bd:'#e2e8f0', icon:XCircle }
  const d = (Date.now() - new Date(iso).getTime()) / 86_400_000
  if (d < 1) return { label:'Today', color:C.green, bg:C.greenBg, bd:C.greenBd, icon:CheckCircle2 }
  if (d < 3) return { label:`${Math.floor(d)}d ago`, color:C.amber, bg:C.amberBg, bd:C.amberBd, icon:AlertTriangle }
  return { label:`${Math.floor(d)}d ago`, color:C.red, bg:C.redBg, bd:C.redBd, icon:XCircle }
}

function fmtDate(iso) {
  if (!iso) return ''
  const dt = new Date(iso)
  const d=dt.getDate().toString().padStart(2,'0'), m=dt.toLocaleString('en',{month:'short'})
  const h=dt.getHours(), mi=dt.getMinutes().toString().padStart(2,'0')
  return `${d} ${m}, ${h%12||12}:${mi}${h>=12?'p':'a'}`
}

function fmtCount(n) { return n==null?'—':n.toLocaleString('en-IN') }

const IBtn = ({icon:Icon,title,onClick,color,bg,bd}) => (
  <button onClick={onClick} title={title} style={{
    display:'inline-flex',alignItems:'center',justifyContent:'center',
    width:22,height:22,borderRadius:4,border:`1px solid ${bd}`,
    background:bg,color,cursor:'pointer',padding:0,flexShrink:0,
  }}><Icon size={10}/></button>
)

/* Inline group selector — fixed-position dropdown to escape overflow:hidden */
function GroupTag({ currentGroup, allGroups, onChange }) {
  const [open, setOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [adding, setAdding] = useState(false)
  const [pos, setPos] = useState({top:0,left:0})
  const btnRef = useRef(null)

  const handleOpen = (e) => {
    e.stopPropagation()
    if (open) { setOpen(false); return }
    const rect = btnRef.current.getBoundingClientRect()
    setPos({ top: rect.bottom + 2, left: rect.left })
    setOpen(true)
    setAdding(false)
  }

  return (
    <span style={{display:'inline-flex'}}>
      <button ref={btnRef} onClick={handleOpen} style={{
        display:'inline-flex',alignItems:'center',gap:2,
        padding:'0 5px',borderRadius:4,fontSize:8,fontWeight:600,lineHeight:'15px',
        background:'#f1f5f9',color:C.textSub,border:`1px solid #e2e8f0`,
        cursor:'pointer',whiteSpace:'nowrap',
      }}>
        <FolderOpen size={7}/>{currentGroup}
      </button>
      {open && ReactDOM.createPortal(
        <>
          <div style={{position:'fixed',inset:0,zIndex:9998}} onClick={()=>setOpen(false)}/>
          <div style={{
            position:'fixed',top:pos.top,left:pos.left,zIndex:9999,
            background:'#fff',border:`1px solid ${C.cardBorder}`,borderRadius:6,
            boxShadow:'0 8px 24px rgba(0,0,0,.15)',minWidth:160,maxHeight:220,
            display:'flex',flexDirection:'column',
          }}>
            {/* Current group — highlighted */}
            <div style={{
              padding:'5px 10px',fontSize:10,fontWeight:700,color:C.primary,
              background:C.primaryLight,borderBottom:`1px solid ${C.cardBorder}`,
              display:'flex',alignItems:'center',gap:5,
            }}>
              <CheckCircle2 size={9}/> {currentGroup}
            </div>
            {/* Other groups */}
            <div style={{overflowY:'auto',flex:1}}>
              {allGroups.filter(g=>g!==currentGroup).map(g=>(
                <button key={g} onClick={()=>{onChange(g);setOpen(false)}} style={{
                  display:'flex',alignItems:'center',gap:5,width:'100%',
                  padding:'5px 10px',border:'none',background:'transparent',
                  fontSize:10,color:C.text,cursor:'pointer',textAlign:'left',
                }}
                  onMouseEnter={e=>e.currentTarget.style.background='#f1f5f9'}
                  onMouseLeave={e=>e.currentTarget.style.background='transparent'}
                >
                  <FolderOpen size={9} style={{color:C.textMuted}}/>{g}
                </button>
              ))}
            </div>
            {/* New group */}
            {!adding ? (
              <button onClick={()=>{setAdding(true);setNewName('')}} style={{
                display:'flex',alignItems:'center',gap:5,width:'100%',
                padding:'6px 10px',border:'none',borderTop:`1px solid ${C.cardBorder}`,
                background:'transparent',fontSize:10,color:C.primary,cursor:'pointer',textAlign:'left',fontWeight:600,
              }}
                onMouseEnter={e=>e.currentTarget.style.background=C.primaryLight}
                onMouseLeave={e=>e.currentTarget.style.background='transparent'}
              >
                <Plus size={9}/> New Group
              </button>
            ) : (
              <div style={{padding:'5px 8px',borderTop:`1px solid ${C.cardBorder}`,display:'flex',gap:3}}>
                <input value={newName} onChange={e=>setNewName(e.target.value)}
                  autoFocus placeholder="Group name..."
                  onKeyDown={e=>{if(e.key==='Enter'&&newName.trim()){onChange(newName.trim());setOpen(false)}if(e.key==='Escape')setOpen(false)}}
                  style={{flex:1,padding:'3px 6px',borderRadius:4,border:`1px solid ${C.inputBorder}`,fontSize:10,color:C.text,outline:'none',minWidth:0}}/>
                <button onClick={()=>{if(newName.trim()){onChange(newName.trim());setOpen(false)}}} style={{
                  padding:'3px 6px',borderRadius:4,border:'none',background:C.primary,color:'#fff',fontSize:9,fontWeight:600,cursor:'pointer',
                }}>OK</button>
              </div>
            )}
          </div>
        </>,
        document.body
      )}
    </span>
  )
}

/* ── Add-table modal ───────────────────────────────────────────────────────── */
function AddTableModal({ open, onClose, onAdd, availableTables, existingGroups, loadingTables }) {
  const [search, setSearch]       = useState('')
  const [selected, setSelected]   = useState(null)
  const [displayName, setDisplayName] = useState('')
  const [groupName, setGroupName] = useState('')
  const [showNewGroup, setShowNewGroup] = useState(false)

  useEffect(() => { if (open) { setSearch(''); setSelected(null); setDisplayName(''); setGroupName(existingGroups[0]||''); setShowNewGroup(false) } }, [open, existingGroups])
  if (!open) return null
  const filtered = availableTables.filter(t => t.toLowerCase().includes(search.toLowerCase()))

  return (
    <div style={{position:'fixed',inset:0,zIndex:1000,display:'flex',alignItems:'center',justifyContent:'center',background:'rgba(0,0,0,.35)'}} onClick={onClose}>
      <div onClick={e=>e.stopPropagation()} style={{background:C.cardBg,borderRadius:12,width:460,maxHeight:'80vh',display:'flex',flexDirection:'column',boxShadow:'0 20px 40px rgba(0,0,0,.15)',border:`1px solid ${C.cardBorder}`}}>
        {/* Header */}
        <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',padding:'12px 16px',borderBottom:`1px solid ${C.cardBorder}`}}>
          <h3 style={{margin:0,fontSize:13,fontWeight:700,color:C.text}}>Add Table to Checklist</h3>
          <button onClick={onClose} style={{background:'none',border:'none',cursor:'pointer',color:C.textMuted,padding:2}}><X size={14}/></button>
        </div>

        {/* Group selector */}
        <div style={{padding:'8px 16px',borderBottom:`1px solid ${C.cardBorder}`,display:'flex',gap:6,alignItems:'center',flexWrap:'wrap'}}>
          <label style={{fontSize:10,fontWeight:600,color:C.textSub,textTransform:'uppercase',letterSpacing:'.04em'}}>Group:</label>
          {!showNewGroup ? (
            <>
              <select value={groupName} onChange={e=>setGroupName(e.target.value)}
                style={{padding:'4px 6px',borderRadius:5,border:`1px solid ${C.inputBorder}`,fontSize:11,color:C.text,outline:'none',flex:1,minWidth:120}}>
                {existingGroups.map(g=><option key={g} value={g}>{g}</option>)}
                {existingGroups.length===0 && <option value="">—</option>}
              </select>
              <button onClick={()=>setShowNewGroup(true)} style={{
                padding:'3px 8px',borderRadius:4,fontSize:10,fontWeight:600,
                border:`1px solid ${C.primaryBd}`,background:C.primaryLight,color:C.primary,cursor:'pointer',
              }}>+ New Group</button>
            </>
          ) : (
            <>
              <input value={groupName} onChange={e=>setGroupName(e.target.value)} placeholder="New group name..."
                autoFocus style={{padding:'4px 6px',borderRadius:5,border:`1px solid ${C.inputBorder}`,fontSize:11,color:C.text,outline:'none',flex:1}}/>
              <button onClick={()=>setShowNewGroup(false)} style={{
                padding:'3px 6px',borderRadius:4,fontSize:10,border:`1px solid ${C.cardBorder}`,background:C.cardBg,color:C.textSub,cursor:'pointer',
              }}>Cancel</button>
            </>
          )}
        </div>

        {/* Search */}
        <div style={{padding:'8px 16px',borderBottom:`1px solid ${C.cardBorder}`}}>
          <div style={{position:'relative'}}>
            <Search size={12} style={{position:'absolute',left:8,top:'50%',transform:'translateY(-50%)',color:C.textMuted}}/>
            <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search tables..."
              style={{width:'100%',padding:'6px 8px 6px 28px',borderRadius:6,border:`1px solid ${C.inputBorder}`,fontSize:12,color:C.text,outline:'none',boxSizing:'border-box'}}/>
          </div>
        </div>

        {/* Table list */}
        <div style={{flex:1,overflowY:'auto',padding:'6px 8px',maxHeight:240}}>
          {loadingTables ? (
            <div style={{textAlign:'center',padding:20,color:C.textMuted,fontSize:11}}>
              <RefreshCw size={14} style={{animation:'spin 1s linear infinite',display:'block',margin:'0 auto 6px'}}/>Loading...
            </div>
          ) : filtered.length===0 ? (
            <div style={{textAlign:'center',padding:20,color:C.textMuted,fontSize:11}}>No tables found.</div>
          ) : filtered.map(t=>(
            <button key={t} onClick={()=>{setSelected(t);setDisplayName(t)}} style={{
              display:'flex',alignItems:'center',gap:6,width:'100%',
              padding:'4px 10px',borderRadius:5,marginBottom:1,
              border:selected===t?`2px solid ${C.primary}`:'2px solid transparent',
              background:selected===t?C.primaryLight:'transparent',
              cursor:'pointer',textAlign:'left',fontSize:11,color:C.text,
            }}>
              <Database size={11} style={{flexShrink:0,color:selected===t?C.primary:C.textMuted}}/>
              <code style={{fontFamily:'Consolas,monospace',fontSize:11,fontWeight:600,color:C.codeColor}}>{t}</code>
            </button>
          ))}
        </div>

        {/* Display name */}
        {selected && (
          <div style={{padding:'8px 16px',borderTop:`1px solid ${C.cardBorder}`}}>
            <label style={{fontSize:10,fontWeight:600,color:C.textSub,textTransform:'uppercase',letterSpacing:'.05em'}}>Display Name</label>
            <input value={displayName} onChange={e=>setDisplayName(e.target.value)}
              style={{width:'100%',padding:'5px 8px',borderRadius:5,marginTop:3,border:`1px solid ${C.inputBorder}`,fontSize:12,color:C.text,outline:'none',boxSizing:'border-box'}}/>
          </div>
        )}

        {/* Footer */}
        <div style={{display:'flex',justifyContent:'flex-end',gap:6,padding:'8px 16px',borderTop:`1px solid ${C.cardBorder}`}}>
          <button onClick={onClose} style={{padding:'5px 12px',borderRadius:6,fontSize:11,fontWeight:600,border:`1px solid ${C.cardBorder}`,background:C.cardBg,color:C.textSub,cursor:'pointer'}}>Cancel</button>
          <button disabled={!selected} onClick={()=>{onAdd(selected,displayName,groupName);onClose()}}
            style={{padding:'5px 12px',borderRadius:6,fontSize:11,fontWeight:600,border:'none',cursor:selected?'pointer':'not-allowed',background:selected?C.primary:'#e2e8f0',color:selected?'#fff':C.textMuted}}>
            <Plus size={11} style={{verticalAlign:'middle',marginRight:3}}/> Add
          </button>
        </div>
      </div>
    </div>
  )
}

/* ── Main page ─────────────────────────────────────────────────────────────── */
export default function ChecklistPage() {
  const navigate = useNavigate()
  const [items,    setItems]    = useState([])
  const [groups,   setGroups]   = useState([])
  const [loading,  setLoading]  = useState(false)
  const [showAdd,  setShowAdd]  = useState(false)
  const [availTbl, setAvailTbl] = useState([])
  const [existGrp, setExistGrp] = useState([])
  const [loadTbl,  setLoadTbl]  = useState(false)
  const [collapsed, setCollapsed] = useState({})   // { groupName: true/false }

  const toggleGroup = (gName) => setCollapsed(prev => ({ ...prev, [gName]: !prev[gName] }))

  const loadData = useCallback(async () => {
    setLoading(true)
    try {
      const {data}=await checklistAPI.getItems()
      setItems(data.data.items||[])
      setGroups(data.data.groups||[])
    } catch {} finally { setLoading(false) }
  }, [])
  useEffect(() => { loadData() }, [loadData])

  const openAdd = async () => {
    setShowAdd(true); setLoadTbl(true)
    try {
      const {data}=await checklistAPI.getAvailableTables()
      setAvailTbl(data.data.tables||[])
      setExistGrp(data.data.groups||[])
    } catch { setAvailTbl([]); setExistGrp([]) } finally { setLoadTbl(false) }
  }
  const handleAdd = async (tn,dn,gn) => {
    try { await checklistAPI.addItem({table_name:tn,display_name:dn,group_name:gn||null}); toast.success(`Added ${dn}`); loadData() }
    catch(e) { toast.error(e.response?.data?.detail||'Failed') }
  }
  const handleDelete = async (id,name) => {
    if (!confirm(`Remove "${name}"?`)) return
    try { await checklistAPI.deleteItem(id); toast.success('Removed'); loadData() } catch { toast.error('Failed') }
  }
  const handleToggle = async (item) => {
    try { await checklistAPI.updateItem(item.id,{is_active:!item.is_active}); loadData() } catch { toast.error('Failed') }
  }
  const handleStamp = async (tn) => {
    try { await checklistAPI.stamp(tn); toast.success('Marked done'); loadData() } catch { toast.error('Failed') }
  }
  const handleGroupChange = async (item, newGroup) => {
    try { await checklistAPI.updateItem(item.id,{group_name:newGroup}); loadData() } catch { toast.error('Failed') }
  }
  const moveItem = async (idx,dir,groupItems) => {
    // Only reorder within the full items list
    const item = groupItems[idx]
    const swapItem = groupItems[idx+dir]
    if (!swapItem) return
    // Swap sort_order values
    try {
      await checklistAPI.reorder([
        {id:item.id, sort_order:swapItem.sort_order},
        {id:swapItem.id, sort_order:item.sort_order},
      ])
      loadData()
    } catch { loadData() }
  }

  const activeCount = items.filter(i=>i.is_active).length
  const freshCount  = items.filter(i=>i.is_active&&i.last_checked_at&&freshness(i.last_checked_at).color===C.green).length
  const staleCount  = items.filter(i=>i.is_active&&(!i.last_checked_at||freshness(i.last_checked_at).color===C.red)).length

  // Group items
  const grouped = {}
  items.forEach(it => {
    const g = it.group_name || 'Ungrouped'
    if (!grouped[g]) grouped[g] = []
    grouped[g].push(it)
  })
  const groupOrder = groups.length > 0 ? groups : Object.keys(grouped)

  return (
    <div style={{color:C.text,fontFamily:'inherit'}}>

      {/* Header */}
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8,gap:8}}>
        <div style={{display:'flex',alignItems:'center',gap:8}}>
          <ClipboardList size={15} color={C.primary}/>
          <span style={{fontSize:13,fontWeight:700,color:C.text}}>Data Checklist</span>
          {[
            {v:items.length,l:'total',c:C.text,bg:'#f1f5f9',bd:'#e2e8f0'},
            {v:activeCount,l:'active',c:C.green,bg:C.greenBg,bd:C.greenBd},
            {v:freshCount,l:'fresh',c:C.green,bg:C.greenBg,bd:C.greenBd},
            {v:staleCount,l:'stale',c:C.red,bg:C.redBg,bd:C.redBd},
          ].map(s=>(
            <span key={s.l} style={{padding:'1px 6px',borderRadius:8,fontSize:9,fontWeight:700,background:s.bg,color:s.c,border:`1px solid ${s.bd}`}}>{s.v} {s.l}</span>
          ))}
        </div>
        <div style={{display:'flex',gap:5}}>
          <button onClick={loadData} disabled={loading} style={{
            display:'flex',alignItems:'center',gap:3,padding:'3px 8px',borderRadius:5,fontSize:10,fontWeight:600,
            cursor:'pointer',border:`1px solid ${C.amberBd}`,background:C.amberBg,color:C.amber,opacity:loading?0.5:1,
          }}>
            <RefreshCw size={10} style={{animation:loading?'spin 1s linear infinite':'none'}}/> Refresh
          </button>
          <button onClick={openAdd} style={{
            display:'flex',alignItems:'center',gap:3,padding:'3px 8px',borderRadius:5,fontSize:10,fontWeight:600,
            cursor:'pointer',border:'none',background:C.primary,color:'#fff',
          }}>
            <Plus size={10}/> Add Table
          </button>
        </div>
      </div>

      {/* Card */}
      <div style={{background:C.cardBg,border:`1px solid ${C.cardBorder}`,borderRadius:8,overflow:'hidden',boxShadow:'0 1px 2px rgba(0,0,0,.05)'}}>

        {loading ? (
          <div style={{textAlign:'center',padding:30,color:C.textMuted,fontSize:10}}>
            <RefreshCw size={12} style={{display:'block',margin:'0 auto 4px',animation:'spin 1s linear infinite'}}/>Loading...
          </div>
        ) : items.length===0 ? (
          <div style={{textAlign:'center',padding:30,color:C.textMuted,fontSize:10}}>
            No tables. Click <strong>Add Table</strong> to start.
          </div>
        ) : (
          <div style={{overflowX:'auto'}}>
            <table style={{width:'100%',borderCollapse:'collapse',fontSize:11,tableLayout:'fixed'}}>
              <colgroup>
                <col style={{width:36}}/>
                <col style={{width:'35%'}}/>
                <col style={{width:70}}/>
                <col style={{width:170}}/>
                <col style={{width:36}}/>
                <col style={{width:155}}/>
              </colgroup>
              <thead>
                <tr style={{background:'#f1f5f9',borderBottom:`1.5px solid ${C.cardBorder}`}}>
                  <th style={{padding:'5px 6px',textAlign:'center',fontSize:9,fontWeight:700,color:C.textSub}}>#</th>
                  <th style={{padding:'5px 8px',textAlign:'left',fontSize:9,fontWeight:700,color:C.textSub}}>TABLE</th>
                  <th style={{padding:'5px 8px',textAlign:'right',fontSize:9,fontWeight:700,color:C.textSub}}>ROWS</th>
                  <th style={{padding:'5px 8px',textAlign:'center',fontSize:9,fontWeight:700,color:C.textSub}}>LAST CHECKED</th>
                  <th style={{padding:'5px 4px'}}></th>
                  <th style={{padding:'5px 6px',textAlign:'center',fontSize:9,fontWeight:700,color:C.textSub}}>ACTIONS</th>
                </tr>
              </thead>
              <tbody>
                {groupOrder.map((gName, gi) => {
                  const gItems = grouped[gName] || []
                  if (gItems.length === 0) return null
                  const gc = GROUP_COLORS[gi % GROUP_COLORS.length]
                  return [
                    /* Group header row — clickable to expand/collapse */
                    <tr key={`gh-${gName}`} style={{background:gc.bg,borderBottom:`1px solid ${gc.bd}`,cursor:'pointer',userSelect:'none'}}
                      onClick={()=>toggleGroup(gName)}>
                      <td colSpan={6} style={{padding:'4px 8px'}}>
                        <div style={{display:'flex',alignItems:'center',gap:5}}>
                          <div style={{width:3,height:14,borderRadius:2,background:gc.bar,flexShrink:0}}/>
                          <ChevronRight size={11} style={{
                            color:gc.fg,flexShrink:0,transition:'transform .2s',
                            transform:collapsed[gName]?'rotate(0deg)':'rotate(90deg)',
                          }}/>
                          <FolderOpen size={11} style={{color:gc.fg,flexShrink:0}}/>
                          <span style={{fontSize:10,fontWeight:700,color:gc.fg,textTransform:'uppercase',letterSpacing:'.04em'}}>{gName}</span>
                          <span style={{fontSize:9,color:gc.fg,opacity:.7,fontWeight:500}}>({gItems.length})</span>
                        </div>
                      </td>
                    </tr>,
                    /* Group items — hidden when collapsed */
                    ...(!collapsed[gName] ? gItems : []).map((row,idx) => {
                      const f=freshness(row.last_checked_at), FI=f.icon
                      return (
                        <tr key={row.id} style={{
                          borderBottom:`1px solid ${C.cardBorder}`,
                          borderLeft:`3px solid ${gc.bar}`,
                          background:idx%2===0?C.cardBg:C.rowAlt,
                          opacity:row.is_active?1:.5,
                          height:28,
                        }}>
                          {/* # */}
                          <td style={{padding:'0 4px',textAlign:'center',verticalAlign:'middle'}}>
                            <div style={{display:'flex',alignItems:'center',justifyContent:'center',gap:0}}>
                              <button onClick={()=>moveItem(idx,-1,gItems)} disabled={idx===0}
                                style={{background:'none',border:'none',cursor:idx===0?'default':'pointer',color:idx===0?'#ddd':C.textSub,padding:0,lineHeight:1}}><ChevronUp size={9}/></button>
                              <span style={{fontSize:9,color:C.textMuted,fontWeight:600,minWidth:12,textAlign:'center'}}>{idx+1}</span>
                              <button onClick={()=>moveItem(idx,1,gItems)} disabled={idx===gItems.length-1}
                                style={{background:'none',border:'none',cursor:idx===gItems.length-1?'default':'pointer',color:idx===gItems.length-1?'#ddd':C.textSub,padding:0,lineHeight:1}}><ChevronDown size={9}/></button>
                            </div>
                          </td>

                          {/* Table */}
                          <td style={{padding:'0 8px',verticalAlign:'middle',overflow:'hidden'}}>
                            <div style={{display:'flex',alignItems:'center',gap:5,whiteSpace:'nowrap'}}>
                              <span style={{fontWeight:600,color:C.text,fontSize:11,overflow:'hidden',textOverflow:'ellipsis'}}>{row.display_name}</span>
                              {!row.table_exists && <span style={{fontSize:8,fontWeight:700,color:C.red,background:C.redBg,padding:'0 3px',borderRadius:3,flexShrink:0}}>!</span>}
                              <GroupTag currentGroup={row.group_name} allGroups={groups} onChange={g=>handleGroupChange(row,g)}/>
                            </div>
                          </td>

                          {/* Rows */}
                          <td style={{padding:'0 8px',textAlign:'right',verticalAlign:'middle'}}>
                            <span style={{fontWeight:600,color:C.text,fontFamily:'Consolas,monospace',fontSize:10}}>{fmtCount(row.row_count)}</span>
                          </td>

                          {/* Last checked */}
                          <td style={{padding:'0 6px',textAlign:'center',verticalAlign:'middle',whiteSpace:'nowrap'}}>
                            {row.last_checked_at ? (
                              <span style={{display:'inline-flex',alignItems:'center',gap:4}}>
                                <span style={{fontSize:9.5,color:C.textSub}}>{fmtDate(row.last_checked_at)}</span>
                                <span style={{
                                  display:'inline-flex',alignItems:'center',gap:2,
                                  padding:'0 5px',borderRadius:8,fontSize:8.5,fontWeight:700,lineHeight:'16px',
                                  background:f.bg,color:f.color,border:`1px solid ${f.bd}`,
                                }}><FI size={7}/>{f.label}</span>
                              </span>
                            ) : (
                              <span style={{fontSize:9,color:C.textMuted,fontStyle:'italic'}}>Not checked</span>
                            )}
                          </td>

                          {/* Active toggle */}
                          <td style={{padding:'0 2px',textAlign:'center',verticalAlign:'middle'}}>
                            <button onClick={()=>handleToggle(row)} style={{
                              width:26,height:13,borderRadius:7,position:'relative',
                              display:'inline-block',cursor:'pointer',border:'none',
                              background:row.is_active?'#10b981':'#e2e8f0',transition:'background .2s',verticalAlign:'middle',
                            }}>
                              <span style={{
                                position:'absolute',top:1.5,width:10,height:10,borderRadius:'50%',
                                background:'#fff',boxShadow:'0 1px 2px rgba(0,0,0,.2)',
                                transition:'left .2s',left:row.is_active?14:1.5,
                              }}/>
                            </button>
                          </td>

                          {/* Actions */}
                          <td style={{padding:'0 6px',textAlign:'center',verticalAlign:'middle'}}>
                            <div style={{display:'flex',gap:2,justifyContent:'center'}}>
                              <IBtn icon={CircleCheck} title="Mark done" onClick={()=>handleStamp(row.table_name)} color={C.green} bg={C.greenBg} bd={C.greenBd}/>
                              {row.table_exists && <IBtn icon={Eye} title="View data" onClick={()=>navigate(`/tables/${encodeURIComponent(row.table_name)}?from=checklist`)} color={C.primary} bg={C.primaryLight} bd={C.primaryBd}/>}
                              {row.table_exists && <IBtn icon={Edit3} title="Edit data" onClick={()=>navigate(`/editor?table=${encodeURIComponent(row.table_name)}&from=checklist`)} color={'#7c3aed'} bg={'#f5f3ff'} bd={'#ddd6fe'}/>}
                              {row.table_exists && <IBtn icon={Upload} title="Upload" onClick={()=>navigate(`/upload?table=${encodeURIComponent(row.table_name)}&from=checklist`)} color={C.amber} bg={C.amberBg} bd={C.amberBd}/>}
                              {row.table_exists && <IBtn icon={Download} title="Export data" onClick={()=>navigate(`/export?table=${encodeURIComponent(row.table_name)}&from=checklist`)} color={'#0284c7'} bg={'#f0f9ff'} bd={'#bae6fd'}/>}
                              <IBtn icon={Trash2} title="Remove" onClick={()=>handleDelete(row.id,row.display_name)} color={C.red} bg={C.redBg} bd={C.redBd}/>
                            </div>
                          </td>
                        </tr>
                      )
                    })
                  ]
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Footer */}
        {!loading && items.length>0 && (
          <div style={{padding:'4px 10px',borderTop:`1px solid ${C.cardBorder}`,background:C.headerBg,fontSize:9,display:'flex',justifyContent:'space-between',alignItems:'center'}}>
            <span style={{color:C.textSub}}><strong>{activeCount}</strong> active of <strong>{items.length}</strong> in {groupOrder.length} group{groupOrder.length!==1?'s':''}</span>
            {staleCount>0 && <span style={{color:C.red,fontWeight:600}}>{staleCount} need updating</span>}
          </div>
        )}
      </div>

      <AddTableModal open={showAdd} onClose={()=>setShowAdd(false)} onAdd={handleAdd}
        availableTables={availTbl} existingGroups={existGrp} loadingTables={loadTbl}/>
      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
