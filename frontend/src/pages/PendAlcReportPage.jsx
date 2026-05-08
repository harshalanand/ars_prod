/**
 * PendAlcReportPage — Pending Allocation Report
 * Filters from full DB, column visibility toggle, total qty
 */
import { useState, useEffect, useMemo, useRef } from 'react'
import { reportsAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { Download, RefreshCw, Table2, Search, X, Filter, Eye, EyeOff } from 'lucide-react'
import { C } from '@/theme/colors'

// Default visible columns (ARS_PEND_ALC table only — no master product join)
const DEFAULT_VISIBLE = ['RDC','ST_CD','MATNR','QTY','MAJ_CAT','GEN_ART_NUMBER','CLR','ALLOC_MODE','PEND_QTY']

export default function PendAlcReportPage() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [downloading, setDownloading] = useState(false)

  // Filters
  const [filters, setFilters] = useState({})
  const [filterCol, setFilterCol] = useState('')
  const [filterSearch, setFilterSearch] = useState('')
  const [filterOptions, setFilterOptions] = useState([])
  const [filterLoading, setFilterLoading] = useState(false)
  const [searchAll, setSearchAll] = useState('')
  const [dropdownOpen, setDropdownOpen] = useState(false)

  // Column visibility
  const [visibleCols, setVisibleCols] = useState(new Set(DEFAULT_VISIBLE))
  const [showColPicker, setShowColPicker] = useState(false)

  const loadRef = useRef(0)
  const load = async (filterOverride) => {
    const id = ++loadRef.current
    setLoading(true)
    try {
      const f = filterOverride !== undefined ? filterOverride : filters
      console.log('Loading with filters:', JSON.stringify(f))
      const { data: res } = await reportsAPI.getPendAlc(5000, f)
      if (loadRef.current !== id) return // stale response
      setData(res.data)
      if (res.data?.columns?.length && visibleCols.size === DEFAULT_VISIBLE.length) {
        setVisibleCols(new Set(DEFAULT_VISIBLE.filter(c => res.data.columns.includes(c))))
      }
    } catch { if (loadRef.current === id) { toast.error('Failed to load'); setData({columns:[],total_rows:0,total_qty:0,preview:[]}) } }
    finally { if (loadRef.current === id) setLoading(false) }
  }
  useEffect(() => { load({}) }, [])

  // Load distinct values from DB when filter column changes
  useEffect(() => {
    if (!filterCol) { setFilterOptions([]); return }
    setFilterLoading(true)
    reportsAPI.getDistinctValues(filterCol)
      .then(r => setFilterOptions(r.data?.data?.values || []))
      .catch(() => setFilterOptions([]))
      .finally(() => setFilterLoading(false))
  }, [filterCol])

  // Filtered options (searchable)
  const filteredOptions = useMemo(() => {
    if (!filterSearch) return filterOptions
    const q = filterSearch.toLowerCase()
    return filterOptions.filter(v => v.toLowerCase().includes(q))
  }, [filterOptions, filterSearch])

  // Data comes pre-filtered from server — only apply searchAll client-side
  const filtered = useMemo(() => {
    if (!data?.preview) return []
    if (!searchAll.trim()) return data.preview
    const q = searchAll.toLowerCase()
    return data.preview.filter(r => Object.values(r).some(v => String(v??'').toLowerCase().includes(q)))
  }, [data, searchAll])

  const totalFilteredQty = useMemo(() => filtered.reduce((s,r) => s + (Number(r.QTY)||0), 0), [filtered])

  const addFilter = (col, val) => {
    const next = {...filters, [col]: [...new Set([...(filters[col]||[]), val])]}
    setFilters(next)
    load(next)
  }
  const removeFilter = (col, val) => {
    const v = (filters[col]||[]).filter(x=>x!==val)
    const next = {...filters}
    if (!v.length) delete next[col]; else next[col] = v
    setFilters(next)
    load(next)
  }
  const clearFilters = () => { setFilters({}); setSearchAll(''); setFilterCol(''); setDropdownOpen(false); load({}) }
  const toggleCol = (c) => setVisibleCols(s => { const n=new Set(s); n.has(c)?n.delete(c):n.add(c); return n })
  const selectAllCols = () => setVisibleCols(new Set(data?.columns||[]))
  const selectDefaultCols = () => setVisibleCols(new Set(DEFAULT_VISIBLE))

  const handleDownload = async () => {
    setDownloading(true); toast('Preparing download…')
    try {
      const res = await reportsAPI.downloadPendAlc(filters)
      const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([res.data]))
      a.download = Object.keys(filters).length ? 'pend_alc_filtered.csv' : 'pending_allocation_report.csv'
      a.click(); toast.success('Done')
    } catch { toast.error('Download failed') }
    finally { setDownloading(false) }
  }

  const activeFilterCount = Object.values(filters).reduce((s,v) => s+v.length, 0)
  const displayCols = (data?.columns||[]).filter(c => visibleCols.has(c))

  return (
    <div style={{color:C.text}}>
      {/* Header */}
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'start',marginBottom:14}}>
        <div>
          <h1 style={{fontSize:20,fontWeight:800,margin:0,display:'flex',alignItems:'center',gap:10}}>
            <Table2 size={20} color={C.primary}/> Pending Allocation Report
          </h1>
          <p style={{fontSize:12,color:C.textMuted,margin:'4px 0 0'}}>ARS_PEND_ALC</p>
        </div>
        <div style={{display:'flex',gap:8}}>
          <button onClick={()=>setShowColPicker(!showColPicker)} style={{
            display:'flex',alignItems:'center',gap:5,padding:'8px 14px',borderRadius:8,
            fontSize:12,fontWeight:700,border:`1px solid ${C.primaryBd}`,background:showColPicker?C.primaryLight:'#fff',color:C.primary,cursor:'pointer',
          }}>{showColPicker?<EyeOff size={13}/>:<Eye size={13}/>} Columns ({visibleCols.size})</button>
          <button onClick={load} disabled={loading} style={{
            display:'flex',alignItems:'center',gap:5,padding:'8px 14px',borderRadius:8,
            fontSize:12,fontWeight:700,border:`1px solid ${C.cardBorder}`,background:'#fff',color:C.textSub,cursor:'pointer',
          }}><RefreshCw size={13} style={loading?{animation:'spin 1s linear infinite'}:{}}/> Refresh</button>
          <button onClick={handleDownload} disabled={downloading||!data?.total_rows} style={{
            display:'flex',alignItems:'center',gap:5,padding:'8px 14px',borderRadius:8,
            fontSize:12,fontWeight:700,border:`1px solid ${C.greenBd}`,background:C.greenBg,color:C.green,cursor:downloading?'wait':'pointer',
          }}>{downloading?<RefreshCw size={13} style={{animation:'spin 1s linear infinite'}}/>:<Download size={13}/>}
            {activeFilterCount>0?`Export Filtered (${data?.total_rows?.toLocaleString()||0})`:'Download All'}
          </button>
        </div>
      </div>

      {/* Column visibility picker */}
      {showColPicker && (
        <div style={{background:C.cardBg,border:`1px solid ${C.cardBorder}`,borderRadius:10,padding:12,marginBottom:12}}>
          <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
            <span style={{fontSize:12,fontWeight:700,color:C.textSub}}>Toggle Columns</span>
            <div style={{display:'flex',gap:6}}>
              <button onClick={selectAllCols} style={{fontSize:10,fontWeight:700,color:C.primary,background:'none',border:'none',cursor:'pointer'}}>All</button>
              <button onClick={selectDefaultCols} style={{fontSize:10,fontWeight:700,color:C.textMuted,background:'none',border:'none',cursor:'pointer'}}>Default</button>
            </div>
          </div>
          <div style={{display:'flex',flexWrap:'wrap',gap:5}}>
            {(data?.columns||[]).map(c => {
              const on = visibleCols.has(c)
              return <button key={c} onClick={()=>toggleCol(c)} style={{
                padding:'4px 10px',borderRadius:14,fontSize:11,fontWeight:600,cursor:'pointer',
                border:`1.5px solid ${on?C.primary:'#e2e8f0'}`,background:on?C.primary:'#fff',color:on?'#fff':C.textMuted,
              }}>{c}</button>
            })}
          </div>
        </div>
      )}

      {/* Filter bar */}
      <div style={{background:C.cardBg,border:`1px solid ${C.cardBorder}`,borderRadius:10,padding:'10px 14px',marginBottom:10,display:'flex',gap:8,flexWrap:'wrap',alignItems:'center'}}>
        <Filter size={13} color={C.textMuted}/>
        <div style={{position:'relative',minWidth:180}}>
          <Search size={12} style={{position:'absolute',left:8,top:'50%',transform:'translateY(-50%)',color:C.textMuted}}/>
          <input value={searchAll} onChange={e=>setSearchAll(e.target.value)} placeholder="Search all…"
            style={{padding:'5px 8px 5px 26px',borderRadius:6,border:`1px solid ${C.inputBorder}`,fontSize:12,color:C.text,width:'100%',boxSizing:'border-box'}}/>
        </div>

        <select value={filterCol} onChange={e=>{setFilterCol(e.target.value);setFilterSearch('');setDropdownOpen(true)}}
          style={{padding:'5px 8px',borderRadius:6,border:`1px solid ${C.inputBorder}`,fontSize:12,color:C.text}}>
          <option value="">+ Column filter</option>
          {(data?.columns||[]).map(c=><option key={c} value={c}>{c}{filters[c]?` (${filters[c].length})`:''}</option>)}
        </select>

        {filterCol && dropdownOpen && (
          <div style={{position:'relative',minWidth:220}}>
            <div style={{display:'flex',alignItems:'center',gap:4}}>
              <div style={{position:'relative',flex:1}}>
                <Search size={12} style={{position:'absolute',left:8,top:'50%',transform:'translateY(-50%)',color:C.textMuted}}/>
                <input value={filterSearch} onChange={e=>setFilterSearch(e.target.value)} autoFocus
                  placeholder={filterLoading?'Loading…':`Search ${filterCol}…`}
                  style={{padding:'5px 8px 5px 26px',borderRadius:6,border:`1px solid ${C.primaryBd}`,fontSize:12,color:C.primary,width:'100%',boxSizing:'border-box'}}/>
              </div>
              <button onClick={()=>{setDropdownOpen(false);setFilterCol('')}} style={{background:'none',border:'none',cursor:'pointer',padding:2}}>
                <X size={14} color={C.textMuted}/>
              </button>
            </div>
            <div style={{position:'absolute',top:'100%',left:0,right:0,zIndex:50,background:'#fff',border:`1px solid ${C.cardBorder}`,borderRadius:8,boxShadow:'0 4px 12px rgba(0,0,0,.1)',marginTop:2,maxHeight:250,overflowY:'auto'}}>
              {filteredOptions.slice(0,200).map(v=>{
                const checked = (filters[filterCol]||[]).includes(v)
                return (
                  <div key={v} onClick={()=>checked?removeFilter(filterCol,v):addFilter(filterCol,v)} style={{
                    padding:'5px 10px',cursor:'pointer',fontSize:12,display:'flex',alignItems:'center',gap:8,
                    background:checked?C.primaryLight:'#fff',
                  }}>
                    <span style={{width:15,height:15,borderRadius:3,border:`1.5px solid ${checked?C.primary:'#d1d5db'}`,
                      background:checked?C.primary:'#fff',display:'flex',alignItems:'center',justifyContent:'center',
                      color:'#fff',fontSize:10,fontWeight:800,flexShrink:0}}>{checked?'✓':''}</span>
                    {v||'(empty)'}
                  </div>
                )
              })}
              {filteredOptions.length>200 && <div style={{padding:'5px 10px',fontSize:11,color:C.textMuted}}>+{filteredOptions.length-200} more — type to search</div>}
              {filteredOptions.length===0 && !filterLoading && <div style={{padding:'10px',fontSize:12,color:C.textMuted,textAlign:'center'}}>No values found</div>}
              {filterLoading && <div style={{padding:'10px',fontSize:12,color:C.textMuted,textAlign:'center'}}>Loading…</div>}
            </div>
          </div>
        )}

        {activeFilterCount>0 && (
          <button onClick={clearFilters} style={{padding:'4px 10px',borderRadius:6,fontSize:11,fontWeight:700,border:'1px solid #fecaca',background:'#fef2f2',color:'#dc2626',cursor:'pointer'}}>
            Clear ({activeFilterCount})
          </button>
        )}

        {Object.entries(filters).map(([col,vals])=>vals.map(v=>(
          <span key={`${col}-${v}`} style={{display:'flex',alignItems:'center',gap:3,padding:'3px 8px',borderRadius:12,fontSize:10,fontWeight:600,background:C.primaryLight,color:C.primary,border:`1px solid ${C.primaryBd}`}}>
            {col}:{v||'(empty)'}
            <button onClick={()=>removeFilter(col,v)} style={{background:'none',border:'none',cursor:'pointer',padding:0}}><X size={10} color={C.primary}/></button>
          </span>
        )))}
      </div>

      {/* Stats */}
      <div style={{display:'flex',gap:12,marginBottom:10}}>
        <div style={{background:C.cardBg,border:`1px solid ${C.cardBorder}`,borderRadius:8,padding:'8px 16px',display:'flex',alignItems:'center',gap:8}}>
          <span style={{fontSize:20,fontWeight:800,color:C.primary}}>{data?.total_rows?.toLocaleString()||0}</span>
          <span style={{fontSize:11,color:C.textMuted}}>Total Rows</span>
        </div>
        <div style={{background:C.cardBg,border:`1px solid ${C.cardBorder}`,borderRadius:8,padding:'8px 16px',display:'flex',alignItems:'center',gap:8}}>
          <span style={{fontSize:20,fontWeight:800,color:C.green}}>{data?.total_qty?.toLocaleString()||0}</span>
          <span style={{fontSize:11,color:C.textMuted}}>Total QTY</span>
        </div>
        {searchAll.trim() && (
          <>
            <div style={{background:C.cardBg,border:`1px solid ${C.cardBorder}`,borderRadius:8,padding:'8px 16px',display:'flex',alignItems:'center',gap:8}}>
              <span style={{fontSize:20,fontWeight:800,color:C.amber}}>{filtered.length.toLocaleString()}</span>
              <span style={{fontSize:11,color:C.textMuted}}>Search Match</span>
            </div>
          </>
        )}
      </div>

      {/* Table */}
      <div style={{background:C.cardBg,border:`1px solid ${C.cardBorder}`,borderRadius:12,overflow:'hidden'}}>
        {loading?(
          <div style={{padding:40,textAlign:'center',color:C.textMuted}}>
            <RefreshCw size={20} style={{animation:'spin 1s linear infinite',margin:'0 auto 8px',display:'block'}}/> Loading…
          </div>
        ):!filtered.length?(
          <div style={{padding:40,textAlign:'center',color:C.textMuted}}>No data{activeFilterCount>0?' matching filters':''}</div>
        ):(
          <div style={{overflowX:'auto',maxHeight:'60vh'}}>
            <table style={{width:'100%',borderCollapse:'collapse',fontSize:11}}>
              <thead style={{position:'sticky',top:0,zIndex:1}}>
                <tr style={{background:'#f1f5f9'}}>
                  <th style={{padding:'6px 8px',fontSize:10,fontWeight:700,color:C.textMuted,borderBottom:`2px solid ${C.cardBorder}`,width:36}}>#</th>
                  {displayCols.map(c=>(
                    <th key={c} onClick={()=>{setFilterCol(c);setFilterSearch('');setDropdownOpen(true)}} style={{
                      padding:'6px 8px',textAlign:'left',fontSize:10,fontWeight:700,whiteSpace:'nowrap',cursor:'pointer',
                      color:filters[c]?C.primary:C.textSub, background:filters[c]?C.primaryLight:'#f1f5f9',
                      borderBottom:`2px solid ${C.cardBorder}`,
                    }}>{c}{filters[c]?` (${filters[c].length})`:''}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.slice(0,2000).map((row,i)=>(
                  <tr key={i} style={{borderBottom:`1px solid ${C.cardBorder}`,background:i%2===0?'#fff':'#fafbfc'}}>
                    <td style={{padding:'4px 8px',fontSize:10,color:C.textMuted,textAlign:'center'}}>{i+1}</td>
                    {displayCols.map(c=>(
                      <td key={c} style={{padding:'4px 8px',whiteSpace:'nowrap',maxWidth:180,overflow:'hidden',textOverflow:'ellipsis'}}>
                        {row[c]!=null?String(row[c]):''}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            {(data?.total_rows||0) > filtered.length && (
              <div style={{padding:'8px 16px',background:C.headerBg,borderTop:`1px solid ${C.cardBorder}`,fontSize:11,color:C.amber,fontWeight:600}}>
                Showing {Math.min(2000, filtered.length).toLocaleString()} of {(data?.total_rows||0).toLocaleString()} total. Download for all data.
              </div>
            )}
          </div>
        )}
      </div>
      <style>{`@keyframes spin{to{transform:rotate(360deg);}}`}</style>
    </div>
  )
}
