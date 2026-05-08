/**
 * ScheduleAuditPage — paged audit log for ARS_STORE_BDC_SCHEDULE_AUDIT.
 *
 * Field-level history of every schedule change: who, when, what changed,
 * old value → new value, source (UI / CSV_IMPORT / API).
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { History, RefreshCw, Search, X } from 'lucide-react'
import DataGrid from '@/components/DataGrid'

const C = {
  primary: '#4f46e5', green: '#16a34a', red: '#dc2626', blue: '#0891b2',
  amber: '#d97706', text: '#1e293b', textSub: '#64748b', textMuted: '#94a3b8',
  border: '#e2e8f0', bg: '#f8fafc', card: '#ffffff',
}

const ACTION_BADGE = {
  INSERT: { bg: '#dcfce7', fg: C.green },
  UPDATE: { bg: '#dbeafe', fg: C.blue },
  DELETE: { bg: '#fee2e2', fg: C.red },
}

export default function ScheduleAuditPage() {
  // Top-bar filters (merged into the DataGrid's params)
  const [stCdFilter,    setStCdFilter]    = useState('')
  const [userFilter,    setUserFilter]    = useState('')
  const [batchFilter,   setBatchFilter]   = useState('')
  const [dateFrom,      setDateFrom]      = useState('')
  const [dateTo,        setDateTo]        = useState('')
  const [bumpKey,       setBumpKey]       = useState(0)

  const fetcher = useCallback(async (gridParams) => {
    const params = { ...gridParams }
    if (stCdFilter.trim())  params.st_cd    = stCdFilter.trim()
    if (userFilter.trim())  params.user     = userFilter.trim()
    if (batchFilter.trim()) params.batch_id = batchFilter.trim()
    if (dateFrom)           params.date_from = dateFrom
    if (dateTo)             params.date_to   = dateTo
    return pendAlcAPI.scheduleAudit(params)
  }, [stCdFilter, userFilter, batchFilter, dateFrom, dateTo])

  const refreshKey = useMemo(
    () => `${stCdFilter}|${userFilter}|${batchFilter}|${dateFrom}|${dateTo}|${bumpKey}`,
    [stCdFilter, userFilter, batchFilter, dateFrom, dateTo, bumpKey]
  )

  const clearAll = () => {
    setStCdFilter(''); setUserFilter(''); setBatchFilter('')
    setDateFrom(''); setDateTo(''); setBumpKey(k => k + 1)
  }

  return (
    <div style={{ padding:'16px 20px', fontFamily:'Inter,system-ui,sans-serif',
                  fontSize:11, color:C.text, background:C.bg, minHeight:'100vh' }}>
      {/* Header */}
      <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:14 }}>
        <History size={16} color={C.primary}/>
        <div>
          <div style={{ fontSize:13, fontWeight:800 }}>Schedule Audit Log</div>
          <div style={{ fontSize:10, color:C.textMuted }}>
            Field-level audit trail of every change to ARS_STORE_BDC_SCHEDULE
          </div>
        </div>
        <div style={{ flex:1 }}/>
        <button onClick={() => setBumpKey(k => k + 1)} style={btn(C.border, '#fff', C.textSub)}>
          <RefreshCw size={11}/> Refresh
        </button>
      </div>

      {/* Filter bar */}
      <div style={{ background:C.card, border:`1px solid ${C.border}`, borderRadius:8,
                    padding:10, marginBottom:10, display:'flex', gap:8, alignItems:'center',
                    flexWrap:'wrap' }}>
        <FilterInput label="Store"    value={stCdFilter}  onChange={setStCdFilter}  width={120}/>
        <FilterInput label="User"     value={userFilter}  onChange={setUserFilter}  width={140}/>
        <FilterInput label="Batch ID" value={batchFilter} onChange={setBatchFilter} width={150}/>
        <span style={{ fontSize:10, color:C.textSub, marginLeft:6 }}>From</span>
        <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
          style={dateInputStyle()}/>
        <span style={{ fontSize:10, color:C.textSub }}>To</span>
        <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
          style={dateInputStyle()}/>
        <button onClick={() => setBumpKey(k => k + 1)} style={btn(C.primary, C.primary, '#fff')}>
          Apply
        </button>
        <button onClick={clearAll} style={btn(C.border, '#fff', C.textSub)}>
          Clear
        </button>
      </div>

      {/* DataGrid */}
      <DataGrid
        fetcher={fetcher}
        refreshKey={refreshKey}
        defaultPageSize={100}
        defaultSortBy="change_time"
        defaultSortDir="desc"
        compact
        emptyText="No audit entries match the filters."
        rowKey={r => r.log_id}
        columns={[
          { key:'log_id',      label:'#',       sortable:true,
            render:r => <span style={{fontFamily:'monospace', fontSize:9, color:C.textMuted}}>
              #{r.log_id}</span> },
          { key:'change_time', label:'WHEN',    sortable:true,
            render:r => <span style={{fontSize:9, color:C.textMuted, whiteSpace:'nowrap'}}>
              {r.change_time?.replace('T', ' ').slice(0, 19)}</span> },
          { key:'st_cd',       label:'ST_CD',   sortable:true, filterType:'text',
            render:r => <span style={{fontFamily:'monospace', fontWeight:700}}>{r.st_cd}</span> },
          { key:'action',      label:'ACTION',  sortable:true, filterType:'multi',
            filterOptions:['INSERT','UPDATE','DELETE'],
            render:r => {
              const s = ACTION_BADGE[r.action] || { bg:'#f1f5f9', fg:C.textSub }
              return <span style={{fontSize:8, fontWeight:700, padding:'2px 7px',
                                   borderRadius:3, background:s.bg, color:s.fg}}>{r.action}</span>
            } },
          { key:'source',      label:'SOURCE',  sortable:true, filterType:'multi',
            filterOptions:['UI','CSV_IMPORT','API'],
            render:r => <span style={{fontSize:9, color:C.textSub}}>{r.source}</span> },
          { key:'user',        label:'USER',    sortable:true, filterType:'text',
            render:r => <span style={{fontSize:10}}>{r.user || '—'}</span> },
          { key:'field',       label:'FIELD',   sortable:true, filterType:'multi',
            filterOptions:['ST_NAME','MON','TUE','WED','THU','FRI','SAT','IS_ACTIVE'],
            render:r => <span style={{fontFamily:'monospace', fontWeight:700, fontSize:10,
                                       color:C.primary}}>{r.field}</span> },
          { key:'old_value',   label:'OLD',     sortable:false, align:'right',
            render:r => <span style={{color:C.textMuted, textDecoration:'line-through',
                                       fontFamily:'monospace', fontSize:10}}>
              {r.old_value ?? '—'}</span> },
          { key:'new_value',   label:'NEW',     sortable:false,
            render:r => <span style={{color:C.text, fontWeight:700,
                                       fontFamily:'monospace', fontSize:10}}>
              {r.new_value ?? '—'}</span> },
          { key:'batch_id',    label:'BATCH',   sortable:true, filterType:'text',
            render:r => r.batch_id
              ? <span style={{fontFamily:'monospace', fontSize:9, color:C.textSub,
                              cursor:'pointer'}}
                  title="Click to filter to just this batch"
                  onClick={() => { setBatchFilter(r.batch_id); setBumpKey(k => k + 1) }}>
                  {r.batch_id}
                </span>
              : <span style={{color:C.textMuted}}>—</span> },
          { key:'note',        label:'NOTE',    sortable:false,
            render:r => r.note
              ? <span style={{fontSize:10, color:C.textSub}}>{r.note}</span>
              : <span style={{color:C.textMuted}}>—</span> },
        ]}
      />
    </div>
  )
}

function FilterInput({ label, value, onChange, width = 140 }) {
  return (
    <div style={{ position:'relative' }}>
      <Search size={11} style={{ position:'absolute', left:8, top:7, color:C.textMuted }}/>
      <input value={value} onChange={e => onChange(e.target.value)}
        placeholder={label}
        style={{ fontSize:11, padding:'5px 22px 5px 26px', borderRadius:4,
                 border:`1px solid ${C.border}`, width, outline:'none' }}/>
      {value && (
        <X size={10} onClick={() => onChange('')}
          style={{ position:'absolute', right:8, top:7, color:C.textMuted, cursor:'pointer' }}/>
      )}
    </div>
  )
}

const btn = (border, bg, color) => ({
  fontSize:10, fontWeight:700, padding:'5px 12px', borderRadius:4,
  border:`1px solid ${border}`, background:bg, color,
  cursor:'pointer', display:'inline-flex', alignItems:'center', gap:5,
})
const dateInputStyle = () => ({
  fontSize:11, padding:'4px 6px', borderRadius:4,
  border:`1px solid ${C.border}`, outline:'none',
})
