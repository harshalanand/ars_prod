/**
 * StoreBdcSchedulePage — manage Mon-Sat BDC schedule per store.
 *
 * Layout:
 *   - Stats row (total / daily / 3× / 2× / 1× / inactive / EXTRA / MISSING)
 *   - Filter bar (search + pattern + active + master-status + hub + RDC + presets + import)
 *   - Stores grouped by HUB (from Master_ALC_INPUT_ST_MASTER), each group
 *     can be expanded/collapsed.
 *   - Each row shows MASTER_STATUS badge (OK / EXTRA / MISSING), RDC,
 *     HUB, ST_STATUS plus the editable Mon-Sat schedule.
 *
 * "EXTRA" rows = in schedule but not in master (typo / orphan)
 * "MISSING" rows = in master but no schedule yet (needs config — has no
 *                  Mon-Sat data, just a stub for the user to fill)
 */
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  CalendarDays, Plus, Save, Trash2, RefreshCw, Search, X, Upload, Download,
  ChevronUp, ChevronDown, ChevronRight, AlertTriangle, AlertCircle, CheckCircle2,
  History,
} from 'lucide-react'

const C = {
  primary: '#4f46e5', green: '#16a34a', red: '#dc2626', blue: '#0891b2',
  amber: '#d97706', text: '#1e293b', textSub: '#64748b', textMuted: '#94a3b8',
  border: '#e2e8f0', bg: '#f8fafc', card: '#ffffff',
}

const DAYS = [
  { key: 'mon', label: 'Mon' },
  { key: 'tue', label: 'Tue' },
  { key: 'wed', label: 'Wed' },
  { key: 'thu', label: 'Thu' },
  { key: 'fri', label: 'Fri' },
  { key: 'sat', label: 'Sat' },
]

const NEW_ROW = (st_cd = '') => ({
  st_cd, st_name: '',
  mon: false, tue: false, wed: false, thu: false, fri: false, sat: false,
  is_active: true,
  rdc: null, hub: null, st_status: null, master_status: 'EXTRA',
  _new: true, _dirty: true,
})

// Derive a human-readable pattern label from the 6 day flags.
function patternOf(r) {
  if (r.master_status === 'MISSING') return 'NOT SET'
  if (r.is_active === false) return 'Inactive'
  const days = DAYS.filter(d => r[d.key]).map(d => d.label.toUpperCase())
  if (days.length === 6) return 'Daily'
  if (days.length === 0) return 'None'
  if (days.length === 3 && r.mon && r.wed && r.fri) return 'M/W/F'
  if (days.length === 2 && r.tue && r.thu)          return 'T/Th'
  return days.join('/')
}

const PATTERN_COLOR = {
  Daily:    C.green,
  'M/W/F':  C.blue,
  'T/Th':   C.primary,
  Inactive: C.textMuted,
  None:     C.amber,
  'NOT SET': C.amber,
}

const STATUS_BADGE = {
  OK:      { bg: '#dcfce7', fg: C.green,    label: 'OK',      icon: CheckCircle2 },
  EXTRA:   { bg: '#fef3c7', fg: C.amber,    label: 'EXTRA',   icon: AlertTriangle },
  MISSING: { bg: '#fee2e2', fg: C.red,      label: 'MISSING', icon: AlertCircle },
}

export default function StoreBdcSchedulePage() {
  const [rows, setRows]       = useState([])
  const [loading, setLoading] = useState(false)
  const [saving, setSaving]   = useState(false)

  // Filters
  const [search, setSearch]   = useState('')
  const [patternFilter, setPatternFilter] = useState('')
  const [activeFilter, setActiveFilter]   = useState('all')
  const [statusFilter, setStatusFilter]   = useState('all')   // all | OK | EXTRA | MISSING
  const [hubFilter, setHubFilter]         = useState('all')
  const [rdcFilter, setRdcFilter]         = useState('all')

  // Sort
  const [sortBy, setSortBy]   = useState('st_cd')
  const [sortDir, setSortDir] = useState('asc')

  // Stat-card filter — one of: '' | 'daily' | 'threex' | 'twox' | 'onex' |
  // 'inactive' | 'extra' | 'missing' | 'total'.
  // Click a card → set; click the same card again → clear.
  const [cardFilter, setCardFilter] = useState('')

  // After a CSV import, remember the filename + source so the next Save
  // tags the audit rows with source='CSV_IMPORT'. Reset to UI after Save.
  const [pendingSource, setPendingSource] = useState({ source: 'UI', note: null })

  // Per-row history drawer
  const [historyFor,    setHistoryFor]    = useState(null)   // { st_cd } when open
  const [historyData,   setHistoryData]   = useState([])
  const [historyLoading,setHistoryLoading]= useState(false)

  const handleCardClick = (key) => {
    if (key === 'total') {
      // TOTAL clears every filter (including the dropdowns and search box).
      setCardFilter(''); setSearch(''); setPatternFilter('')
      setActiveFilter('all'); setStatusFilter('all'); setHubFilter('all'); setRdcFilter('all')
      return
    }
    setCardFilter(prev => prev === key ? '' : key)
  }

  const fileRef = useRef()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await pendAlcAPI.scheduleList()
      setRows((data?.data || []).map(r => ({ ...r, _dirty: false, _new: false })))
    } catch {
      toast.error('Failed to load schedule')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  // Distinct HUBs and RDCs for the filter dropdowns
  const distinctHubs = useMemo(() => {
    const set = new Set()
    rows.forEach(r => r.hub && set.add(r.hub))
    return [...set].sort()
  }, [rows])
  const distinctRdcs = useMemo(() => {
    const set = new Set()
    rows.forEach(r => r.rdc && set.add(r.rdc))
    return [...set].sort()
  }, [rows])

  // Stats
  const stats = useMemo(() => {
    const out = { total: rows.length, daily: 0, threex: 0, twox: 0, onex: 0,
                  inactive: 0, extra: 0, missing: 0 }
    rows.forEach(r => {
      if (r.master_status === 'EXTRA')   out.extra++
      if (r.master_status === 'MISSING') { out.missing++; return }  // missing rows have no day flags
      if (r.is_active === false) { out.inactive++; return }
      const n = DAYS.filter(d => r[d.key]).length
      if (n === 6) out.daily++
      else if (n === 3) out.threex++
      else if (n === 2) out.twox++
      else if (n === 1) out.onex++
    })
    return out
  }, [rows])

  // Distinct patterns for the dropdown
  const patternOptions = useMemo(() => {
    const set = new Set()
    rows.forEach(r => set.add(patternOf(r)))
    return [...set].filter(p => p !== 'None').sort()
  }, [rows])

  // Filtered + sorted view
  const filtered = useMemo(() => {
    let arr = rows
    if (search) {
      const q = search.toLowerCase()
      arr = arr.filter(r =>
        (r.st_cd || '').toLowerCase().includes(q) ||
        (r.st_name || '').toLowerCase().includes(q) ||
        (r.hub || '').toLowerCase().includes(q) ||
        (r.rdc || '').toLowerCase().includes(q)
      )
    }
    if (patternFilter) arr = arr.filter(r => patternOf(r) === patternFilter)
    if (activeFilter === 'active')   arr = arr.filter(r => r.is_active !== false && r.master_status !== 'MISSING')
    if (activeFilter === 'inactive') arr = arr.filter(r => r.is_active === false)
    if (statusFilter !== 'all')      arr = arr.filter(r => r.master_status === statusFilter)
    if (hubFilter !== 'all')         arr = arr.filter(r => (r.hub || '(no hub)') === hubFilter)
    if (rdcFilter !== 'all')         arr = arr.filter(r => (r.rdc || '(no rdc)') === rdcFilter)

    // Stat-card filter — drives day-count or status conditions
    if (cardFilter) {
      const dayCount = (r) => DAYS.filter(d => r[d.key]).length
      if (cardFilter === 'daily')
        arr = arr.filter(r => r.master_status !== 'MISSING' && r.is_active !== false && dayCount(r) === 6)
      else if (cardFilter === 'threex')
        arr = arr.filter(r => r.master_status !== 'MISSING' && r.is_active !== false && dayCount(r) === 3)
      else if (cardFilter === 'twox')
        arr = arr.filter(r => r.master_status !== 'MISSING' && r.is_active !== false && dayCount(r) === 2)
      else if (cardFilter === 'onex')
        arr = arr.filter(r => r.master_status !== 'MISSING' && r.is_active !== false && dayCount(r) === 1)
      else if (cardFilter === 'inactive')
        arr = arr.filter(r => r.is_active === false)
      else if (cardFilter === 'extra')
        arr = arr.filter(r => r.master_status === 'EXTRA')
      else if (cardFilter === 'missing')
        arr = arr.filter(r => r.master_status === 'MISSING')
    }

    arr = [...arr].sort((a, b) => {
      const av = a[sortBy] ?? '', bv = b[sortBy] ?? ''
      if (av < bv) return sortDir === 'asc' ? -1 : 1
      if (av > bv) return sortDir === 'asc' ? 1 : -1
      return 0
    })
    return arr
  }, [rows, search, patternFilter, activeFilter, statusFilter, hubFilter, rdcFilter, cardFilter, sortBy, sortDir])

  const dirtyRows = useMemo(() => rows.filter(r => r._dirty), [rows])

  // ----- mutations ---------------------------------------------------------

  const setField = (st_cd, field, val) => {
    setRows(prev => prev.map(r => {
      if (r.st_cd !== st_cd) return r
      const updated = { ...r, [field]: val, _dirty: true }
      // If this was a MISSING row and the user just set any day,
      // upgrade its status to OK (it's becoming a real schedule).
      if (r.master_status === 'MISSING' && DAYS.some(d => updated[d.key])) {
        updated.master_status = 'OK'
      }
      return updated
    }))
  }
  const toggleDay = (st_cd, day) => {
    const r = rows.find(x => x.st_cd === st_cd)
    if (r) setField(st_cd, day, !r[day])
  }

  const addRow = () => setRows(prev => [NEW_ROW(), ...prev])

  const removeRow = async (r) => {
    if (r._new || r.master_status === 'MISSING') {
      // _new: never persisted. MISSING: only an in-memory placeholder.
      setRows(prev => prev.filter(x => x.st_cd !== r.st_cd))
      return
    }
    if (!confirm(`Delete schedule for ${r.st_cd}?`)) return
    try {
      await pendAlcAPI.scheduleDelete(r.st_cd, { source: 'UI' })
      setRows(prev => prev.filter(x => x.st_cd !== r.st_cd))
      toast.success(`Deleted ${r.st_cd}`)
    } catch { toast.error('Delete failed') }
  }

  const saveDirty = async () => {
    if (dirtyRows.length === 0) { toast('Nothing to save', { icon: 'ℹ️' }); return }
    const bad = dirtyRows.find(r => !r.st_cd?.trim())
    if (bad) { toast.error('All rows need a Store Code'); return }
    setSaving(true)
    try {
      const payload = dirtyRows.map(r => ({
        st_cd: r.st_cd.trim(),
        st_name: r.st_name?.trim() || null,
        mon: !!r.mon, tue: !!r.tue, wed: !!r.wed,
        thu: !!r.thu, fri: !!r.fri, sat: !!r.sat,
        is_active: r.is_active !== false,
      }))
      const { data } = await pendAlcAPI.scheduleUpsert(payload, {
        source: pendingSource.source,
        note:   pendingSource.note,
      })
      const audited = data?.audit_rows_written || 0
      toast.success(
        `Saved ${payload.length} schedule${payload.length > 1 ? 's' : ''}` +
        (audited > 0 ? ` · ${audited} audit entries` : '')
      )
      // After a save, revert source back to UI so subsequent edits log as UI.
      setPendingSource({ source: 'UI', note: null })
      load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Save failed')
    } finally { setSaving(false) }
  }

  const handleCsvUpload = (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = ev => {
      try {
        const lines = ev.target.result.trim().split('\n')
        const hdr = lines[0].split(',').map(h => h.trim().toLowerCase())
        const idx = (n) => hdr.findIndex(h => h === n.toLowerCase())
        const stIdx = idx('st_cd')
        if (stIdx < 0) { toast.error('CSV needs ST_CD column'); return }
        const nameIdx = idx('st_name')
        const dayIdx = Object.fromEntries(DAYS.map(d => [d.key, idx(d.key)]))
        const isTrue = (v) => /^(1|y|yes|true)$/i.test(v || '')
        const newRows = lines.slice(1).filter(l => l.trim()).map(l => {
          const cols = l.split(',').map(c => c.trim())
          return {
            st_cd:   cols[stIdx], st_name: nameIdx >= 0 ? cols[nameIdx] : '',
            mon: isTrue(cols[dayIdx.mon]), tue: isTrue(cols[dayIdx.tue]),
            wed: isTrue(cols[dayIdx.wed]), thu: isTrue(cols[dayIdx.thu]),
            fri: isTrue(cols[dayIdx.fri]), sat: isTrue(cols[dayIdx.sat]),
            is_active: true, _dirty: true, _new: false,
          }
        }).filter(r => r.st_cd)
        setRows(prev => {
          const map = new Map(prev.map(r => [r.st_cd, r]))
          newRows.forEach(r => map.set(r.st_cd, { ...map.get(r.st_cd), ...r, _dirty: true }))
          return [...map.values()]
        })
        // Tag the next Save as a CSV import (with the filename in the note)
        // so audit rows can be filtered by source='CSV_IMPORT'.
        setPendingSource({ source: 'CSV_IMPORT', note: `CSV: ${file.name}` })
        toast.success(`Imported ${newRows.length} row(s) from ${file.name} — click Save to persist`)
      } catch { toast.error('CSV parse failed') }
    }
    reader.readAsText(file)
    e.target.value = ''
  }

  // ---------------------------------------------------------------------
  // Per-row history drawer — pulls from /pend-alc/schedule/audit?st_cd=...
  // ---------------------------------------------------------------------
  const openHistory = async (r) => {
    setHistoryFor(r); setHistoryData([]); setHistoryLoading(true)
    try {
      const { data } = await pendAlcAPI.scheduleAudit({
        st_cd: r.st_cd, page_size: 200,
      })
      setHistoryData(data?.data || [])
    } catch (e) {
      toast.error('Failed to load history')
    } finally { setHistoryLoading(false) }
  }
  const closeHistory = () => { setHistoryFor(null); setHistoryData([]) }

  const downloadTemplate = () => {
    const csv = 'ST_CD,ST_NAME,MON,TUE,WED,THU,FRI,SAT\n'
              + 'HB05,Sample Store 5,1,0,1,0,1,0\n'
              + 'HB08,Sample Store 8,1,1,1,1,1,1\n'
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([csv], { type:'text/csv;charset=utf-8' }))
    a.download = 'BDC_Schedule_Template.csv'
    document.body.appendChild(a); a.click(); document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }

  const cycleSort = (key) => {
    if (sortBy !== key) { setSortBy(key); setSortDir('asc'); return }
    setSortDir(d => d === 'asc' ? 'desc' : 'asc')
  }

  return (
    <div style={{ padding:'16px 20px', fontFamily:'Inter,system-ui,sans-serif',
                  fontSize:11, color:C.text, background:C.bg, minHeight:'100vh' }}>

      {/* Header */}
      <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:12 }}>
        <CalendarDays size={16} color={C.primary}/>
        <div>
          <div style={{ fontSize:13, fontWeight:800 }}>Store BDC Schedule</div>
          <div style={{ fontSize:10, color:C.textMuted }}>
            Mon–Sat schedule per store · synced with Master_ALC_INPUT_ST_MASTER
          </div>
        </div>
        <div style={{ flex:1 }}/>
        <button onClick={load} disabled={loading} style={btn(C.border, '#fff', C.textSub)}>
          <RefreshCw size={11} style={{ animation: loading ? 'spin 1s linear infinite' : 'none' }}/>
          Refresh
        </button>
        <button onClick={saveDirty} disabled={saving || dirtyRows.length === 0}
          style={btn(C.green, C.green + '15', C.green)}>
          <Save size={11}/> Save{dirtyRows.length > 0 ? ` (${dirtyRows.length})` : ''}
        </button>
      </div>

      {/* Stats — clickable filters. Click a card to filter; click again to clear.
            TOTAL clears every filter (search, dropdowns, card). */}
      <div style={{ display:'grid', gridTemplateColumns:'repeat(8,1fr)', gap:8, marginBottom:10 }}>
        <Stat label="TOTAL"    value={stats.total}    color={C.primary}
              filterKey="total"   active={cardFilter === ''} onClick={handleCardClick}
              sub="click to clear"/>
        <Stat label="DAILY"    value={stats.daily}    color={C.green}    sub="6× / week"
              filterKey="daily"   active={cardFilter === 'daily'} onClick={handleCardClick}/>
        <Stat label="3× / wk"  value={stats.threex}   color={C.blue}     sub="M/W/F · etc"
              filterKey="threex"  active={cardFilter === 'threex'} onClick={handleCardClick}/>
        <Stat label="2× / wk"  value={stats.twox}     color={C.primary}  sub="T/Th · etc"
              filterKey="twox"    active={cardFilter === 'twox'} onClick={handleCardClick}/>
        <Stat label="1× / wk"  value={stats.onex}     color={C.amber}    sub="single day"
              filterKey="onex"    active={cardFilter === 'onex'} onClick={handleCardClick}/>
        <Stat label="INACTIVE" value={stats.inactive} color={C.textMuted}
              filterKey="inactive" active={cardFilter === 'inactive'} onClick={handleCardClick}/>
        <Stat label="EXTRA"    value={stats.extra}    color={C.amber}    sub="not in master"
              filterKey="extra"   active={cardFilter === 'extra'} onClick={handleCardClick}/>
        <Stat label="MISSING"  value={stats.missing}  color={C.red}      sub="needs config"
              filterKey="missing" active={cardFilter === 'missing'} onClick={handleCardClick}/>
      </div>

      {/* Active card-filter chip */}
      {cardFilter && (
        <div style={{ marginBottom:10, fontSize:11, color:C.textSub,
                       display:'flex', alignItems:'center', gap:8 }}>
          <span style={{ fontSize:9, fontWeight:700, color:C.textMuted, letterSpacing:'.05em' }}>
            CARD FILTER:
          </span>
          <span style={{ fontSize:10, fontWeight:700, padding:'3px 9px', borderRadius:12,
                          background:C.primary + '22', color:C.primary,
                          display:'inline-flex', alignItems:'center', gap:4 }}>
            {cardFilter.toUpperCase()}
            <X size={10} style={{ cursor:'pointer' }} onClick={() => setCardFilter('')}/>
          </span>
          <span style={{ fontSize:10, color:C.textMuted }}>
            {filtered.length} matching stores
          </span>
        </div>
      )}

      {/* Filter bar */}
      <div style={{ background:C.card, border:`1px solid ${C.border}`, borderRadius:8,
                    padding:10, marginBottom:10, display:'flex', gap:8, alignItems:'center',
                    flexWrap:'wrap' }}>
        <div style={{ position:'relative' }}>
          <Search size={12} style={{ position:'absolute', left:8, top:7, color:C.textMuted }}/>
          <input value={search} onChange={e => setSearch(e.target.value)}
            placeholder="Search code / name / hub / rdc…"
            style={{ fontSize:11, padding:'5px 10px 5px 26px', borderRadius:4,
                     border:`1px solid ${C.border}`, width:220, outline:'none' }}/>
          {search && (
            <X size={11} onClick={() => setSearch('')}
              style={{ position:'absolute', right:8, top:7, color:C.textMuted, cursor:'pointer' }}/>
          )}
        </div>

        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} style={selStyle()}>
          <option value="all">Master: All</option>
          <option value="OK">OK only</option>
          <option value="EXTRA">EXTRA only</option>
          <option value="MISSING">MISSING only</option>
        </select>

        <select value={hubFilter} onChange={e => setHubFilter(e.target.value)} style={selStyle()}>
          <option value="all">HUB: All</option>
          {distinctHubs.map(h => <option key={h} value={h}>{h}</option>)}
          <option value="(no hub)">(no hub)</option>
        </select>

        <select value={rdcFilter} onChange={e => setRdcFilter(e.target.value)} style={selStyle()}>
          <option value="all">RDC: All</option>
          {distinctRdcs.map(r => <option key={r} value={r}>{r}</option>)}
        </select>

        <select value={patternFilter} onChange={e => setPatternFilter(e.target.value)} style={selStyle()}>
          <option value="">Pattern: All</option>
          {patternOptions.map(p => <option key={p} value={p}>{p}</option>)}
        </select>

        <select value={activeFilter} onChange={e => setActiveFilter(e.target.value)} style={selStyle()}>
          <option value="all">Active: All</option>
          <option value="active">Active only</option>
          <option value="inactive">Inactive only</option>
        </select>

        <div style={{ flex:1 }}/>
        <button onClick={() => fileRef.current?.click()} style={btn(C.border, '#fff', C.textSub)}>
          <Upload size={11}/> Import
        </button>
        <button onClick={downloadTemplate} style={btn(C.border, '#fff', C.textSub)}>
          <Download size={11}/> Template
        </button>
        <input ref={fileRef} type="file" accept=".csv"
          onChange={handleCsvUpload} style={{ display:'none' }}/>
        <button onClick={addRow} style={btn(C.primary, C.primary, '#fff')}>
          <Plus size={11}/> Add Store
        </button>
      </div>

      {/* Schedule grouped by HUB */}
      {loading ? (
        <div style={{ background:C.card, border:`1px solid ${C.border}`, borderRadius:8,
                       padding:30, textAlign:'center', color:C.textMuted }}>Loading…</div>
      ) : filtered.length === 0 ? (
        <div style={{ background:C.card, border:`1px solid ${C.border}`, borderRadius:8,
                       padding:30, textAlign:'center', color:C.textMuted }}>
          {rows.length === 0 ? 'No schedules yet — click "Add Store" or import CSV' : 'No stores match the filters'}
        </div>
      ) : (
        <div style={{ background:C.card, border:`1px solid ${C.border}`,
                       borderRadius:8, overflow:'hidden' }}>
          <div style={{ overflowX:'auto', maxHeight:'70vh' }}>
            <table style={{ width:'100%', borderCollapse:'collapse', fontSize:11 }}>
              <thead style={{ position:'sticky', top:0, zIndex:1 }}>
                <tr style={{ background:'#fafbfc' }}>
                  <th style={{ ...th(), width:80, textAlign:'center' }}>STATUS</th>
                  <ColH label="ST_CD"      sk="st_cd"     sortBy={sortBy} sortDir={sortDir} onSort={cycleSort}/>
                  <ColH label="STORE NAME" sk="st_name"   sortBy={sortBy} sortDir={sortDir} onSort={cycleSort}/>
                  <ColH label="HUB"        sk="hub"       sortBy={sortBy} sortDir={sortDir} onSort={cycleSort} width={60}/>
                  <ColH label="RDC"        sk="rdc"       sortBy={sortBy} sortDir={sortDir} onSort={cycleSort} width={50}/>
                  <ColH label="ST STATUS"  sk="st_status" sortBy={sortBy} sortDir={sortDir} onSort={cycleSort} width={70}/>
                  <ColH label="PATTERN"    sortable={false}/>
                  {DAYS.map(d => (
                    <th key={d.key} style={th({ textAlign:'center', width:42 })}>{d.label}</th>
                  ))}
                  <th style={th({ textAlign:'center', width:60 })}>Active</th>
                  <th style={{ ...th(), width:30 }}/>
                </tr>
              </thead>
              <tbody>
                {filtered.map(r => {
                  const pat = patternOf(r)
                  const patColor = PATTERN_COLOR[pat] || C.textSub
                  const sb = STATUS_BADGE[r.master_status] || STATUS_BADGE.OK
                  const isMissing = r.master_status === 'MISSING'
                  const isExtra   = r.master_status === 'EXTRA'
                  return (
                    <tr key={r.st_cd}
                      style={{ borderBottom:`1px solid ${C.border}`,
                               background: r._dirty ? '#fffbeb'
                                            : isMissing ? '#fef2f2'
                                            : isExtra   ? '#fffbf0'
                                            : '#fff' }}>
                      <td style={{ ...td(), textAlign:'center' }}>
                        <span style={{ fontSize:8, fontWeight:700, padding:'2px 7px',
                                        borderRadius:3, background:sb.bg, color:sb.fg,
                                        display:'inline-flex', alignItems:'center', gap:3 }}>
                          <sb.icon size={9}/>{sb.label}
                        </span>
                      </td>
                      <td style={td()}>
                        <input value={r.st_cd}
                          disabled={!r._new}
                          onChange={e => setField(r.st_cd, 'st_cd', e.target.value)}
                          placeholder="HB05"
                          style={{ ...inp, fontFamily:'monospace', fontWeight:700,
                                   background: r._new ? '#fff' : 'transparent',
                                   border: r._new ? `1px solid ${C.border}` : 'none' }}/>
                      </td>
                      <td style={td()}>
                        <input value={r.st_name || ''}
                          onChange={e => setField(r.st_cd, 'st_name', e.target.value)}
                          placeholder="(optional)" style={inp}/>
                      </td>
                      <td style={{ ...td(), fontFamily:'monospace', fontSize:10,
                                    fontWeight:700, color:C.primary }}>
                        {r.hub || '—'}
                      </td>
                      <td style={{ ...td(), color:C.textSub, fontFamily:'monospace', fontSize:10 }}>
                        {r.rdc || '—'}
                      </td>
                      <td style={td()}>
                        {r.st_status
                          ? <span style={{ fontSize:9, fontWeight:600, color:C.textSub }}>{r.st_status}</span>
                          : <span style={{ color:C.textMuted }}>—</span>}
                      </td>
                      <td style={td()}>
                        <span style={{ fontSize:9, fontWeight:700, padding:'2px 7px',
                                        borderRadius:3, background: patColor + '22',
                                        color: patColor }}>
                          {pat}
                        </span>
                      </td>
                      {DAYS.map(d => (
                        <td key={d.key} style={{ ...td(), textAlign:'center' }}>
                          <input type="checkbox"
                            checked={!!r[d.key]}
                            disabled={isExtra}
                            onChange={() => toggleDay(r.st_cd, d.key)}
                            style={{
                              accentColor: C.primary,
                              cursor: isExtra ? 'not-allowed' : 'pointer',
                              opacity: isExtra ? 0.4 : 1,
                            }}/>
                        </td>
                      ))}
                      <td style={{ ...td(), textAlign:'center' }}>
                        <input type="checkbox" checked={r.is_active !== false}
                          disabled={isMissing}
                          onChange={e => setField(r.st_cd, 'is_active', e.target.checked)}/>
                      </td>
                      <td style={{ ...td(), textAlign:'center', whiteSpace:'nowrap' }}>
                        <button
                          onClick={() => openHistory(r)}
                          disabled={r._new || isExtra && r.master_status === 'EXTRA' && !r.st_cd}
                          title="View change history"
                          style={{ background:'none', border:'none',
                                   cursor: r._new ? 'not-allowed' : 'pointer',
                                   color: r._new ? C.textMuted : C.primary,
                                   padding:2, marginRight:2 }}>
                          <History size={11}/>
                        </button>
                        <button onClick={() => removeRow(r)}
                          style={{ background:'none', border:'none', cursor:'pointer',
                                   color:C.red, padding:2 }}>
                          <Trash2 size={11}/>
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* CSV-import indicator banner */}
      {pendingSource.source === 'CSV_IMPORT' && (
        <div style={{ marginTop:8, padding:'6px 12px', borderRadius:4,
                       background: C.amber + '15', border:`1px solid ${C.amber}40`,
                       fontSize:11, color:C.amber, display:'flex', alignItems:'center', gap:8 }}>
          <Upload size={11}/>
          <span>Next Save will be tagged as <b>CSV_IMPORT</b> ({pendingSource.note})</span>
          <span style={{ flex:1 }}/>
          <button onClick={() => setPendingSource({ source:'UI', note:null })}
            style={{ background:'none', border:'none', cursor:'pointer', color:C.amber,
                     fontSize:10, fontWeight:700, textDecoration:'underline' }}>
            Tag as UI instead
          </button>
        </div>
      )}

      {/* History drawer */}
      {historyFor && (
        <div onClick={closeHistory}
          style={{ position:'fixed', inset:0, background:'rgba(0,0,0,.4)', zIndex:50,
                   display:'flex', justifyContent:'flex-end' }}>
          <div onClick={e => e.stopPropagation()}
            style={{ width:640, maxWidth:'95vw', height:'100vh', background:'#fff',
                     boxShadow:'-8px 0 24px rgba(0,0,0,.15)',
                     display:'flex', flexDirection:'column' }}>
            <div style={{ padding:'12px 16px', borderBottom:`1px solid ${C.border}`,
                          display:'flex', alignItems:'center', gap:10 }}>
              <History size={14} color={C.primary}/>
              <div style={{ fontSize:13, fontWeight:800 }}>
                Change History · <code>{historyFor.st_cd}</code>
              </div>
              <span style={{ fontSize:10, color:C.textMuted }}>
                {historyData.length} entries
              </span>
              <div style={{ flex:1 }}/>
              <button onClick={closeHistory}
                style={{ background:'none', border:'none', cursor:'pointer',
                         fontSize:18, color:C.textMuted }}>×</button>
            </div>
            <div style={{ flex:1, overflowY:'auto', padding:'8px 0' }}>
              {historyLoading ? (
                <div style={{ padding:30, textAlign:'center', color:C.textMuted }}>Loading…</div>
              ) : historyData.length === 0 ? (
                <div style={{ padding:30, textAlign:'center', color:C.textMuted }}>
                  No audit entries yet for this store.
                </div>
              ) : (
                <table style={{ width:'100%', borderCollapse:'collapse', fontSize:11 }}>
                  <thead>
                    <tr style={{ background:C.bg }}>
                      <th style={th({ width:120 })}>WHEN</th>
                      <th style={th({ width:60 })}>ACTION</th>
                      <th style={th({ width:70 })}>SOURCE</th>
                      <th style={th({ width:80 })}>USER</th>
                      <th style={th({ width:80 })}>FIELD</th>
                      <th style={th()}>OLD → NEW</th>
                    </tr>
                  </thead>
                  <tbody>
                    {historyData.map(h => (
                      <tr key={h.log_id} style={{ borderBottom:`1px solid ${C.border}` }}>
                        <td style={{ ...td(), fontSize:9, color:C.textMuted, whiteSpace:'nowrap' }}>
                          {h.change_time?.replace('T', ' ').slice(0, 19)}
                        </td>
                        <td style={td()}>
                          <span style={{ fontSize:8, fontWeight:700, padding:'2px 6px',
                                          borderRadius:3,
                                          background: h.action === 'INSERT' ? '#dcfce7'
                                                    : h.action === 'DELETE' ? '#fee2e2'
                                                    : '#dbeafe',
                                          color: h.action === 'INSERT' ? C.green
                                               : h.action === 'DELETE' ? C.red
                                               : C.blue }}>
                            {h.action}
                          </span>
                        </td>
                        <td style={{ ...td(), fontSize:9 }}>{h.source}</td>
                        <td style={{ ...td(), fontSize:10 }}>{h.user || '—'}</td>
                        <td style={{ ...td(), fontFamily:'monospace', fontSize:10, fontWeight:700 }}>{h.field}</td>
                        <td style={td()}>
                          <span style={{ color:C.textMuted, textDecoration:'line-through' }}>
                            {h.old_value ?? '—'}
                          </span>
                          {' → '}
                          <span style={{ color:C.text, fontWeight:600 }}>
                            {h.new_value ?? '—'}
                          </span>
                          {h.note && (
                            <div style={{ fontSize:9, color:C.textMuted, marginTop:2 }}>
                              {h.note}
                            </div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      )}

      <div style={{ marginTop:8, fontSize:10, color:C.textMuted }}>
        Showing {filtered.length} of {rows.length}
        {dirtyRows.length > 0 && (
          <span style={{ color:C.amber, fontWeight:600, marginLeft:8 }}>
            · {dirtyRows.length} unsaved
          </span>
        )}
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}

// ---------------------------------------------------------------------------

function Stat({ label, value, color, sub, filterKey, active, onClick }) {
  const clickable = !!onClick
  return (
    <div
      onClick={clickable ? () => onClick(filterKey) : undefined}
      style={{
        background: active ? color + '12' : C.card,
        border:`1px solid ${active ? color : C.border}`,
        borderRadius:8, padding:'10px 12px',
        borderTop:`3px solid ${color}`,
        cursor: clickable ? 'pointer' : 'default',
        boxShadow: active ? `0 0 0 2px ${color}33` : 'none',
        transition:'all .12s', userSelect:'none',
      }}
      onMouseEnter={e => { if (clickable && !active) e.currentTarget.style.background = '#fafbfc' }}
      onMouseLeave={e => { if (clickable && !active) e.currentTarget.style.background = C.card }}>
      <div style={{ fontSize:9, fontWeight:700, color, letterSpacing:'.06em',
                     marginBottom:3 }}>{label}</div>
      <div style={{ fontSize:18, fontWeight:800, color:C.text, lineHeight:1 }}>{value}</div>
      {sub && <div style={{ fontSize:9, color:C.textMuted, marginTop:3 }}>{sub}</div>}
    </div>
  )
}

function CountChip({ color, label }) {
  return (
    <span style={{ fontSize:9, fontWeight:700, padding:'1px 7px', borderRadius:9,
                    background: color + '22', color }}>{label}</span>
  )
}

function ColH({ label, sk, sortBy, sortDir, onSort, sortable = true, width }) {
  const active = sortable && sortBy === sk
  return (
    <th style={{ ...th({ width }),
                  cursor: sortable ? 'pointer' : 'default',
                  color: active ? C.primary : C.textSub,
                  userSelect: 'none' }}
      onClick={sortable ? () => onSort(sk) : undefined}>
      <span style={{ display:'inline-flex', alignItems:'center', gap:3 }}>
        {label}
        {active && (sortDir === 'asc' ? <ChevronUp size={10}/> : <ChevronDown size={10}/>)}
      </span>
    </th>
  )
}

const inp = {
  fontSize:11, padding:'4px 8px', borderRadius:3,
  width:'100%', outline:'none',
}
const th = (extra = {}) => ({
  padding:'8px 10px', textAlign:'left', fontSize:9, fontWeight:700,
  color:C.textSub, letterSpacing:'.05em', borderBottom:`1px solid ${C.border}`,
  whiteSpace:'nowrap', ...extra,
})
const td = (extra = {}) => ({ padding:'4px 6px', ...extra })
const btn = (border, bg, color) => ({
  fontSize:10, fontWeight:700, padding:'5px 12px', borderRadius:4,
  border:`1px solid ${border}`, background:bg, color,
  cursor:'pointer', display:'inline-flex', alignItems:'center', gap:5,
})
const selStyle = () => ({
  fontSize:11, padding:'5px 8px', borderRadius:4,
  border:`1px solid ${C.border}`, background:'#fff', color:C.text,
  outline:'none', cursor:'pointer',
})
