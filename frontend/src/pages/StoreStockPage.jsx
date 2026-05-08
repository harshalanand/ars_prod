/**
 * StoreStockPage
 * Light theme — matches the rest of the ARS app (bg-gray-50 layout).
 * All text colours are dark so they're readable on white/light cards.
 */
import { useState, useEffect, useCallback } from 'react'
import { storeStockAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  RefreshCw, Save, Search, CheckCircle2, XCircle,
  AlertTriangle, Database, Sparkles
} from 'lucide-react'
import { C } from '@/theme/colors'

/* ── Reusable components ────────────────────────────────────────────────────── */

const StatusBadge = ({ status }) => {
  const colors = status==='Active'  ? { bg:C.greenBg, fg:C.green, bd:C.greenBd }
               : status==='New'     ? { bg:C.amberBg, fg:C.amber, bd:C.amberBd }
               :                      { bg:C.redBg,   fg:C.red,   bd:C.redBd   }
  const Icon   = status==='Active'  ? CheckCircle2
               : status==='New'     ? AlertTriangle
               :                      XCircle
  return (
    <span style={{
      display:'inline-flex', alignItems:'center', gap:3,
      padding:'2px 7px', borderRadius:20, fontSize:9, fontWeight:700,
      background:colors.bg, color:colors.fg, border:`1px solid ${colors.bd}`,
      whiteSpace:'nowrap',
    }}>
      <Icon size={10} style={{flexShrink:0}}/> {status}
    </span>
  )
}

const NewBadge = () => (
  <span style={{
    display:'inline-flex', alignItems:'center', gap:2,
    padding:'1px 5px', borderRadius:20, fontSize:9, fontWeight:700,
    background: C.amberBg, color: C.amber, border:`1px solid ${C.amberBd}`,
  }}>
    <Sparkles size={9}/> New
  </span>
)

const Toggle = ({ active, onClick }) => (
  <button onClick={onClick} style={{
    display:'inline-flex', alignItems:'center', gap:5,
    padding:'3px 8px', borderRadius:6, fontSize:10, fontWeight:700,
    cursor:'pointer',
    border:`1px solid ${active ? C.greenBd : C.redBd}`,
    background: active ? C.greenBg : C.redBg,
    color:      active ? C.green   : C.red,
    whiteSpace:'nowrap', transition:'all .15s',
  }}>
    <span style={{
      width:26, height:13, borderRadius:7, position:'relative',
      display:'inline-block', flexShrink:0, transition:'background .2s',
      background: active ? '#10b981' : '#e2e8f0',
    }}>
      <span style={{
        position:'absolute', top:1.5, width:10, height:10, borderRadius:'50%',
        background:'#fff', boxShadow:'0 1px 2px rgba(0,0,0,.3)',
        transition:'left .2s', left: active ? 14 : 1.5,
      }}/>
    </span>
    <span style={{color: active ? C.green : C.red, fontWeight:700, fontSize:10}}>
      {active ? 'Active' : 'Inactive'}
    </span>
  </button>
)

/* ── Main page ───────────────────────────────────────────────────────────── */
export default function StoreStockPage() {
  const [rows,      setRows]      = useState([])
  const [dirty,     setDirty]     = useState({})
  const [loading,   setLoading]   = useState(false)
  const [syncing,   setSyncing]   = useState(false)
  const [saving,    setSaving]    = useState(false)
  const [search,    setSearch]    = useState('')
  const [filterTab, setFilterTab] = useState('all')
  const [dataDate,  setDataDate]  = useState(null)  // global max date from ET_STORE_STOCK

  // Load merged list (saved + new unsaved SLOCs from ET_STORE_STOCK)
  const loadData = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await storeStockAPI.getSlocSettings()
      setRows(data.data.items || [])
      setDataDate(data.data.data_date || null)
      setDirty({})
    } catch {} finally { setLoading(false) }
  }, [])
  useEffect(() => { loadData() }, [loadData])

  // Manual sync/refresh
  const handleSync = async () => {
    setSyncing(true)
    try {
      await loadData()
      const nc = rows.filter(r => r.is_new).length
      toast.success(nc > 0 ? `${nc} new SLOC(s) need attention` : 'All SLOCs up to date')
    } catch {} finally { setSyncing(false) }
  }

  const setField = (sloc, field, val) =>
    setDirty(p => ({ ...p, [sloc]: { ...(p[sloc]||{}), [field]: val } }))

  const getVal = (row, field) =>
    dirty[row.sloc]?.[field] !== undefined ? dirty[row.sloc][field] : row[field]

  const toggleStatus = (sloc) => {
    const row = rows.find(r => r.sloc === sloc)
    setField(sloc, 'status', getVal(row,'status') === 'Active' ? 'Inactive' : 'Active')
  }

  const handleSave = async () => {
    const keys = Object.keys(dirty)
    if (!keys.length) { toast('Nothing to save.'); return }
    setSaving(true)
    try {
      const items = keys.map(sloc => {
        const base = rows.find(r => r.sloc === sloc) || {}
        return {
          sloc,
          kpi:    dirty[sloc]?.kpi    !== undefined ? dirty[sloc].kpi    : base.kpi,
          status: dirty[sloc]?.status !== undefined ? dirty[sloc].status : base.status,
        }
      })
      const { data } = await storeStockAPI.bulkUpdate(items)
      toast.success(data.message)
      await loadData()
    } catch {} finally { setSaving(false) }
  }

  const visible = rows.filter(r => {
    const q = search.toLowerCase()
    const match = r.sloc.toLowerCase().includes(q) ||
      (getVal(r,'kpi')||'').toLowerCase().includes(q)
    if (!match) return false
    const st = getVal(r,'status')
    if (filterTab==='active')   return st==='Active'
    if (filterTab==='inactive') return st==='Inactive'
    if (filterTab==='new')      return r.is_new || st==='New'
    return true
  })

  const dirtyCount    = Object.keys(dirty).length
  const newCount      = rows.filter(r => r.is_new).length
  const activeCount   = rows.filter(r => getVal(r,'status')==='Active').length
  const inactiveCount = rows.filter(r => getVal(r,'status')==='Inactive').length

  /* ── render ─────────────────────────────────────────────────────────────── */
  return (
    <div style={{ color: C.text, fontFamily:'inherit' }}>

      {/* ── Page title ── */}
      <div style={{ marginBottom:8 }}>
        <h1 style={{
          fontSize:13, fontWeight:700,
          color: C.text, margin:0, display:'flex', alignItems:'center', gap:6,
        }}>
          <Database size={15} color={C.primary} />
          Store Sloc Validation
        </h1>
        <p style={{ fontSize:9, color: C.textSub, marginTop:2, margin:'2px 0 0' }}>
          Configure <strong style={{color:C.text}}>KPI</strong> labels and{' '}
          <strong style={{color:C.text}}>Active / Inactive</strong> status per SLOC.
          &nbsp;Table:&nbsp;
          <code style={{
            background:'#f1f5f9', color: C.primary,
            padding:'1px 5px', borderRadius:3, fontSize:10,
            border:`1px solid ${C.primaryBd}`, fontWeight:600,
          }}>
            ARS_STORE_SLOC_SETTINGS
          </code>
        </p>
      </div>

      {/* ── Data freshness alert ── */}
      {!loading && dataDate && (() => {
        const diffDays = (Date.now() - new Date(dataDate).getTime()) / 86_400_000
        const isOk = diffDays < 2  // day-1 is OK
        const dateFmt = new Date(dataDate).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'})
        return (
          <div style={{
            display:'flex', alignItems:'center', gap:10,
            padding:'5px 10px', marginBottom:8, borderRadius:6,
            background: isOk ? C.greenBg : C.redBg,
            border: `1px solid ${isOk ? C.greenBd : C.redBd}`,
            fontSize:10, color: isOk ? C.green : C.red,
          }}>
            {isOk
              ? <CheckCircle2 size={14} style={{flexShrink:0}}/>
              : <AlertTriangle size={14} style={{flexShrink:0}}/>}
            <span>
              <strong>ET_STORE_STOCK</strong> data date: <strong>{dateFmt}</strong>
              {isOk
                ? ' — Data is up to date.'
                : ` — Data is ${Math.floor(diffDays)} day${Math.floor(diffDays)>1?'s':''} old. Please update the source data.`}
            </span>
          </div>
        )
      })()}
      {!loading && !dataDate && rows.length > 0 && (
        <div style={{
          display:'flex', alignItems:'center', gap:10,
          padding:'5px 10px', marginBottom:8, borderRadius:6,
          background: C.amberBg, border: `1px solid ${C.amberBd}`,
          fontSize:10, color: C.amber,
        }}>
          <AlertTriangle size={14} style={{flexShrink:0}}/>
          <span><strong>ET_STORE_STOCK</strong> — No date column found. Cannot determine data freshness.</span>
        </div>
      )}

      {/* ── Card wrapper ── */}
      <div style={{
        background: C.cardBg, border:`1px solid ${C.cardBorder}`,
        borderRadius:8, overflow:'hidden',
        boxShadow:'0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.06)',
      }}>

        {/* Card header */}
        <div style={{
          display:'flex', justifyContent:'space-between', alignItems:'center',
          flexWrap:'wrap', gap:6, padding:'5px 10px',
          background: C.headerBg, borderBottom:`1px solid ${C.cardBorder}`,
        }}>
          <span style={{ fontSize:10, fontWeight:600, color: C.textSub }}>
            {rows.length} distinct SLOC values from{' '}
            <code style={{ fontSize:10, color: C.primary,
              background: C.primaryLight, padding:'1px 4px', borderRadius:3 }}>
              ET_STORE_STOCK
            </code>
          </span>

          <div style={{ display:'flex', gap:6 }}>
            {/* Sync */}
            <button onClick={handleSync} disabled={syncing||loading} style={{
              display:'flex', alignItems:'center', gap:4,
              padding:'4px 10px', borderRadius:6, fontSize:10, fontWeight:600,
              cursor:'pointer', border:`1px solid ${C.amberBd}`,
              background: C.amberBg, color: C.amber,
              opacity:(syncing||loading)?0.5:1, transition:'all .15s',
            }}>
              <RefreshCw size={10} style={{animation:syncing?'spin 1s linear infinite':'none'}}/>
              Sync New SLOCs
              {newCount > 0 && (
                <span style={{
                  background:C.amber, color:'#fff', borderRadius:99,
                  padding:'0 5px', fontSize:9, fontWeight:800,
                }}>{newCount}</span>
              )}
            </button>

            {/* Save */}
            <button onClick={handleSave} disabled={saving||dirtyCount===0} style={{
              display:'flex', alignItems:'center', gap:4,
              padding:'4px 12px', borderRadius:6, fontSize:10, fontWeight:600,
              cursor: dirtyCount>0 ? 'pointer' : 'not-allowed',
              border:'none',
              background: dirtyCount>0 ? C.primary : '#e2e8f0',
              color:      dirtyCount>0 ? '#fff'    : C.textMuted,
              opacity: saving ? 0.6 : 1,
              boxShadow: dirtyCount>0 ? '0 0 12px rgba(79,70,229,.3)' : 'none',
              transition:'all .15s',
            }}>
              <Save size={10}/>
              Save Changes
              {dirtyCount > 0 && (
                <span style={{
                  background:'rgba(255,255,255,.25)', color:'#fff',
                  borderRadius:99, padding:'0 5px', fontSize:9, fontWeight:800,
                }}>{dirtyCount}</span>
              )}
            </button>
          </div>
        </div>

        {/* Stats strip */}
        <div style={{
          display:'grid', gridTemplateColumns:'repeat(5,1fr)',
          borderBottom:`1px solid ${C.cardBorder}`,
        }}>
          {[
            { label:'Total SLOCs',   value:rows.length,   color:C.text,   bg:'#f8fafc' },
            { label:'Active',        value:activeCount,   color:C.green,  bg:C.greenBg },
            { label:'Inactive',      value:inactiveCount, color:C.red,    bg:C.redBg   },
            { label:'New',           value:newCount,      color:C.amber,  bg:C.amberBg },
            { label:'Unsaved Edits', value:dirtyCount,    color:C.indigo, bg:C.indigoBg },
          ].map((s,i) => (
            <div key={s.label} style={{
              padding:'5px 10px', background:s.bg,
              borderRight: i<4 ? `1px solid ${C.cardBorder}` : 'none',
            }}>
              <div style={{ fontSize:14, fontWeight:800, color:s.color, lineHeight:1 }}>{s.value}</div>
              <div style={{ fontSize:9, color:C.textSub, marginTop:2, fontWeight:500 }}>{s.label}</div>
            </div>
          ))}
        </div>

        {/* Search + filter */}
        <div style={{
          display:'flex', gap:8, flexWrap:'wrap', alignItems:'center',
          padding:'4px 10px', borderBottom:`1px solid ${C.cardBorder}`,
          background: C.headerBg,
        }}>
          <div style={{ position:'relative', flex:1, minWidth:180 }}>
            <Search size={11} style={{
              position:'absolute', left:8, top:'50%', transform:'translateY(-50%)',
              color: C.textMuted,
            }}/>
            <input
              type="text" value={search} placeholder="Search SLOC or KPI…"
              onChange={e => setSearch(e.target.value)}
              style={{
                width:'100%', padding:'4px 6px 4px 24px', borderRadius:4,
                background: C.inputBg, border:`1px solid ${C.inputBorder}`,
                color: C.text,
                fontSize:10, outline:'none', boxSizing:'border-box',
              }}
            />
          </div>

          <div style={{
            display:'flex', background:'#fff',
            border:`1px solid ${C.cardBorder}`, borderRadius:7, padding:3, gap:2,
          }}>
            {[
              { key:'all',      label:'All'                                     },
              { key:'active',   label:'Active'                                  },
              { key:'inactive', label:'Inactive'                                },
              { key:'new',      label:newCount>0 ? `New (${newCount})` : 'New' },
            ].map(f => (
              <button key={f.key} onClick={() => setFilterTab(f.key)} style={{
                padding:'2px 8px', borderRadius:3, fontSize:9, fontWeight:600,
                border:'none', cursor:'pointer', transition:'all .15s',
                background: filterTab===f.key ? C.primary      : 'transparent',
                color:      filterTab===f.key ? '#fff'          : C.textSub,
              }}>
                {f.label}
              </button>
            ))}
          </div>
        </div>

        {/* New SLOC alert */}
        {newCount > 0 && (
          <div style={{
            display:'flex', alignItems:'center', gap:8,
            padding:'6px 12px', background:C.amberBg,
            borderBottom:`1px solid ${C.amberBd}`,
            fontSize:11, color: C.amber,
          }}>
            <AlertTriangle size={14} style={{flexShrink:0}}/>
            <span style={{color:C.amber}}>
              <strong>{newCount} new SLOC{newCount>1?'s':''}</strong> found in ET_STORE_STOCK — needs attention.
              Set KPI, activate, and <strong>Save Changes</strong> to include in grid runs.
            </span>
          </div>
        )}

        {/* Table */}
        <div style={{ overflowX:'auto' }}>
          <table style={{ width:'100%', borderCollapse:'collapse', fontSize:11, minWidth:540 }}>
            <thead>
              <tr style={{
                background:'#f1f5f9',
                borderBottom:`2px solid ${C.cardBorder}`,
              }}>
                {[
                  { label:'SLOC',              align:'left',   width:140 },
                  { label:'KPI',               align:'left',   width:null },
                  { label:'ACTIVE / INACTIVE', align:'center', width:160 },
                  { label:'STATUS',            align:'center', width:100 },
                ].map(h => (
                  <th key={h.label} style={{
                    padding:'5px 10px', textAlign:h.align,
                    fontSize:9, fontWeight:700,
                    color: C.textSub,        /* ← readable header labels */
                    textTransform:'uppercase', letterSpacing:'.06em',
                    width: h.width||undefined,
                  }}>
                    {h.label}
                  </th>
                ))}
              </tr>
            </thead>

            <tbody>
              {loading ? (
                <tr><td colSpan={4} style={{textAlign:'center',padding:60,color:C.textMuted}}>
                  <RefreshCw size={18} style={{
                    display:'block', margin:'0 auto 8px',
                    animation:'spin 1s linear infinite',
                  }}/>
                  Loading SLOC data…
                </td></tr>
              ) : visible.length===0 ? (
                <tr><td colSpan={4} style={{textAlign:'center',padding:60,color:C.textMuted}}>
                  No SLOC records found.
                </td></tr>
              ) : visible.map((row, idx) => {
                const isDirty   = !!dirty[row.sloc]
                const kpiVal    = getVal(row,'kpi') ?? ''
                const statusVal = getVal(row,'status') ?? 'Active'
                const isActive  = statusVal==='Active'

                return (
                  <tr key={row.sloc} style={{
                    borderBottom:`1px solid ${C.cardBorder}`,
                    background: isDirty
                      ? C.indigoBg
                      : idx%2===0 ? C.cardBg : C.rowAlt,
                    transition:'background .12s',
                  }}>

                    {/* SLOC */}
                    <td style={{ padding:'4px 10px' }}>
                      <div style={{ display:'flex', alignItems:'center', gap:5 }}>
                        <code style={{
                          fontFamily:'Consolas,monospace', fontSize:10, fontWeight:700,
                          color: C.codeColor,
                          letterSpacing:'.04em',
                        }}>
                          {row.sloc}
                        </code>
                        {row.is_new && <NewBadge/>}
                        {isDirty && (
                          <span title="Unsaved change" style={{
                            width:6, height:6, borderRadius:'50%',
                            background:C.primary, flexShrink:0,
                          }}/>
                        )}
                      </div>
                    </td>

                    {/* KPI input */}
                    <td style={{ padding:'4px 10px' }}>
                      <input
                        type="text"
                        value={kpiVal}
                        onChange={e => setField(row.sloc,'kpi',e.target.value)}
                        placeholder="Enter KPI label…"
                        style={{
                          width:'100%', padding:'3px 6px', borderRadius:4, fontSize:10,
                          background: isDirty && dirty[row.sloc]?.kpi!==undefined
                            ? C.indigoBg : C.inputBg,
                          border: `1px solid ${
                            isDirty && dirty[row.sloc]?.kpi!==undefined
                              ? C.primary : C.inputBorder
                          }`,
                          color: C.text,     /* ← always dark, always readable */
                          caretColor: C.primary,
                          outline:'none', boxSizing:'border-box', fontFamily:'inherit',
                        }}
                      />
                    </td>

                    {/* Toggle */}
                    <td style={{ padding:'4px 10px', textAlign:'center' }}>
                      <Toggle active={isActive} onClick={() => toggleStatus(row.sloc)}/>
                    </td>

                    {/* Status badge */}
                    <td style={{ padding:'4px 10px', textAlign:'center' }}>
                      <StatusBadge status={statusVal}/>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {/* Card footer */}
        {!loading && rows.length > 0 && (
          <div style={{
            padding:'9px 18px', borderTop:`1px solid ${C.cardBorder}`,
            background: C.headerBg, fontSize:12,
            display:'flex', justifyContent:'space-between', alignItems:'center',
          }}>
            <span style={{color:C.textSub}}>
              Showing <strong style={{color:C.text}}>{visible.length}</strong> of{' '}
              <strong style={{color:C.text}}>{rows.length}</strong> records
            </span>
            {dirtyCount > 0 && (
              <span style={{color:C.amber, fontWeight:600}}>
                ● {dirtyCount} unsaved change{dirtyCount>1?'s':''} — click Save Changes
              </span>
            )}
          </div>
        )}
      </div>

      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
