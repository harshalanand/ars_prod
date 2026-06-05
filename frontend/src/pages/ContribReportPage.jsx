/**
 * ContribReportPage — Full-table paginated report (curated columns).
 *
 *  • Top: Result-table picker · MAJ_CAT / SEG / STATUS filters · vendor search
 *         · "Show all columns" toggle · Refresh
 *  • Grid: server-side paginated (500 rows/page) view of the chosen
 *          Cont_Percentage_* table. Vendor code (M_VND_CD) hidden — only
 *          vendor name shown.
 *  • Footer: pagination controls (← Prev · page X of Y · Next →) + total count.
 */
import { useEffect, useMemo, useState, useCallback, useRef } from 'react'
import { contribAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { ClipboardCheck, RefreshCw, ChevronDown, ChevronLeft, ChevronRight, Search, Filter, X } from 'lucide-react'
import { C } from '@/theme/colors'

const fmtCell = (v, col) => {
  if (v === null || v === undefined || v === '') return ''
  if (typeof v === 'number') {
    if (/CONT%|GAP|ATR|MERCH_INPUT|RMN/.test(col)) {
      return (Math.abs(v) <= 1.5 ? (v * 100).toFixed(2) + '%' : v.toFixed(2))
    }
    return v.toFixed(4)
  }
  return String(v)
}

// Friendly labels for the product-hierarchy / grouping column codes.
// Falls back to the raw code if not in this map.
const HIER_LABELS = {
  M_YARN_02:  'Yarn 2',
  WEAVE_2:    'Weave 2',
  MACRO_MVGR: 'Macro MVGR',
  MICRO_MVGR: 'Micro MVGR',
  M_VND_CD:   'Vendor',
  RNG_SEG:    'Range Seg',
  FAB:        'Fabric',
  CLR:        'Color',
  SZ:         'Size',
}
const hierLabel = (code) => HIER_LABELS[code] ? `${HIER_LABELS[code]} (${code})` : code

// Parse a Cont_Percentage_* table name into its (level, hier, dateOnly, time) parts.
// Handles BOTH legacy month-only tables (YYYY_MM) and new timestamped tables
// (YYYY_MM_DD_HHMM). Time is kept separately so we can dedupe to the latest
// run per (date, level, hier) when multiple executions happened on one day.
//   Cont_Percentage_MACRO_MVGR_2026_06             → store / MACRO_MVGR / dateOnly=2026_06       / time=''
//   Cont_Percentage_MACRO_MVGR_CO_2026_06          → company / MACRO_MVGR / dateOnly=2026_06       / time=''
//   Cont_Percentage_MACRO_MVGR_2026_06_02_1430     → store / MACRO_MVGR / dateOnly=2026_06_02   / time=1430
//   Cont_Percentage_MACRO_MVGR_CO_2026_06_02_1430  → company / MACRO_MVGR / dateOnly=2026_06_02 / time=1430
const TABLE_RE = /^Cont_Percentage_(.+?)(_CO)?_(\d{4}_\d{2}(?:_\d{2}_\d{4})?)$/i
const parseTable = (name) => {
  const m = TABLE_RE.exec(name)
  if (!m) return null
  const ts = m[3]
  const parts = ts.split('_')
  let dateOnly, time
  if (parts.length === 4) {
    // YYYY_MM_DD_HHMM — strip time
    dateOnly = `${parts[0]}_${parts[1]}_${parts[2]}`
    time = parts[3]
  } else {
    // Legacy YYYY_MM month-only table
    dateOnly = ts
    time = ''
  }
  return {
    table_name: name,
    hier: m[1].toUpperCase(),
    level: m[2] ? 'company' : 'store',
    dateOnly,
    time,
  }
}

// Render the date suffix in a friendlier way for the dropdown.
// Now date-only — no time. Multiple executions on the same date collapse to
// one entry (we use the latest one when looking up the table).
//   "2026_06"        → "Jun 2026"
//   "2026_06_02"     → "02 Jun 2026"
const MONTH_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
const dateLabel = (raw) => {
  const parts = raw.split('_')
  if (parts.length === 2) {
    const [y, mo] = parts
    return `${MONTH_SHORT[+mo - 1] || mo} ${y}`
  }
  if (parts.length === 3) {
    const [y, mo, d] = parts
    return `${d} ${MONTH_SHORT[+mo - 1] || mo} ${y}`
  }
  return raw
}


/* Multi-select column-header filter — same UX as ContribReviewPage. */
function FilterDropdown({ column, options, selected, onChange }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const ref = useRef(null)
  const filtered = useMemo(
    () => options.filter(o => (o || '').toLowerCase().includes(q.toLowerCase())),
    [options, q],
  )

  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  const toggle = (v) => onChange(
    column,
    selected.includes(v) ? selected.filter(x => x !== v) : [...selected, v],
  )

  return (
    <div ref={ref} style={{ position: 'relative', display: 'inline-block' }}>
      <button onClick={() => setOpen(!open)} style={{
        padding: '2px 6px', borderRadius: 4, fontSize: 9, fontWeight: 700, cursor: 'pointer',
        border: `1px solid ${selected.length ? C.primary : '#e2e8f0'}`,
        background: selected.length ? C.primaryLight : '#fff',
        color: selected.length ? C.primary : C.textMuted,
        display: 'flex', alignItems: 'center', gap: 2,
      }}>
        <Filter size={8} /> {selected.length ? `${selected.length}` : ''}
        <ChevronDown size={8} />
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, zIndex: 100, background: '#fff',
          border: `1px solid ${C.cardBorder}`, borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,.15)',
          marginTop: 2, width: 200, maxHeight: 280, display: 'flex', flexDirection: 'column',
        }}>
          <div style={{ padding: '6px 8px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', gap: 4, alignItems: 'center' }}>
            <Search size={11} color={C.textMuted} />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder={`Filter ${column}...`} autoFocus
              style={{ flex: 1, border: 'none', outline: 'none', fontSize: 11, background: 'transparent' }} />
            {selected.length > 0 && (
              <button onClick={() => onChange(column, [])} style={{ fontSize: 9, color: C.red, background: 'none', border: 'none', cursor: 'pointer', fontWeight: 700 }}>Clear</button>
            )}
          </div>
          <div style={{ overflowY: 'auto', maxHeight: 220 }}>
            {filtered.map(o => (
              <div key={o} onClick={() => toggle(o)} style={{
                padding: '4px 10px', cursor: 'pointer', fontSize: 11, display: 'flex', alignItems: 'center', gap: 6,
                background: selected.includes(o) ? C.primaryLight : '#fff',
              }}>
                <span style={{
                  width: 14, height: 14, borderRadius: 3, border: `1.5px solid ${selected.includes(o) ? C.primary : '#d1d5db'}`,
                  background: selected.includes(o) ? C.primary : '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
                  color: '#fff', fontSize: 8, fontWeight: 800, flexShrink: 0,
                }}>{selected.includes(o) ? '✓' : ''}</span>
                {o}
              </div>
            ))}
            {filtered.length === 0 && <div style={{ padding: 8, textAlign: 'center', color: C.textMuted, fontSize: 11 }}>No results</div>}
          </div>
        </div>
      )}
    </div>
  )
}


/* Column picker — groups columns by the part before `|` so picking the base
 * (e.g. `0001_STK_Q`) toggles every preset variant (`0001_STK_Q|L7D`,
 * `0001_STK_Q|L30D`, …) in one click. Chevron expands a group to let the
 * user toggle individual variants when they need finer control.
 *
 * Same trigger button + dropdown shell as FilterDropdown so the look stays
 * consistent with the in-header column filters.
 */
function ColumnPicker({ allColumns, selected, onChange }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [expandedBases, setExpandedBases] = useState(() => new Set())
  const ref = useRef(null)

  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  const selectedSet = useMemo(() => new Set(selected), [selected])

  // Group columns by the text before `|`. Columns without `|` form singleton groups.
  const groups = useMemo(() => {
    const m = new Map()
    for (const c of allColumns) {
      const idx = c.indexOf('|')
      const base = idx >= 0 ? c.slice(0, idx) : c
      if (!m.has(base)) m.set(base, [])
      m.get(base).push(c)
    }
    let arr = Array.from(m.entries()).map(([base, members]) => ({ base, members }))
    const needle = q.trim().toLowerCase()
    if (needle) {
      arr = arr.filter(g =>
        g.base.toLowerCase().includes(needle)
        || g.members.some(c => c.toLowerCase().includes(needle)),
      )
    }
    return arr
  }, [allColumns, q])

  const groupState = (members) => {
    let allIn = true, anyIn = false
    for (const c of members) {
      if (selectedSet.has(c)) anyIn = true
      else allIn = false
    }
    return anyIn ? (allIn ? 'all' : 'partial') : 'none'
  }

  // Preserve the original column order in `allColumns` when emitting changes.
  const emit = (nextSet) => onChange(allColumns.filter(c => nextSet.has(c)))

  const toggleGroup = (members) => {
    const state = groupState(members)
    const next = new Set(selectedSet)
    if (state === 'all') for (const c of members) next.delete(c)
    else                 for (const c of members) next.add(c)
    emit(next)
  }
  const toggleMember = (c) => {
    const next = new Set(selectedSet)
    if (next.has(c)) next.delete(c)
    else             next.add(c)
    emit(next)
  }
  const toggleExpand = (base) => {
    setExpandedBases(prev => {
      const next = new Set(prev)
      if (next.has(base)) next.delete(base)
      else                next.add(base)
      return next
    })
  }

  const CheckBox = ({ state }) => (
    <span style={{
      width: 14, height: 14, borderRadius: 3, flexShrink: 0,
      border: `1.5px solid ${state === 'none' ? '#d1d5db' : C.primary}`,
      background: state === 'all'     ? C.primary
                : state === 'partial' ? C.primaryLight
                                       : '#fff',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      color: '#fff', fontSize: 8, fontWeight: 800,
    }}>
      {state === 'all' ? '✓' : state === 'partial' ? '—' : ''}
    </span>
  )

  return (
    <div ref={ref} style={{ position: 'relative', display: 'inline-block' }}>
      <button onClick={() => setOpen(!open)} style={{
        padding: '2px 6px', borderRadius: 4, fontSize: 9, fontWeight: 700, cursor: 'pointer',
        border: `1px solid ${selected.length ? C.primary : '#e2e8f0'}`,
        background: selected.length ? C.primaryLight : '#fff',
        color: selected.length ? C.primary : C.textMuted,
        display: 'flex', alignItems: 'center', gap: 2,
      }}>
        <Filter size={8} /> {selected.length || ''}
        <ChevronDown size={8} />
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: '100%', right: 0, zIndex: 100, background: '#fff',
          border: `1px solid ${C.cardBorder}`, borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,.15)',
          marginTop: 2, width: 280, maxHeight: 360, display: 'flex', flexDirection: 'column',
        }}>
          <div style={{ padding: '6px 8px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', gap: 4, alignItems: 'center' }}>
            <Search size={11} color={C.textMuted} />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter columns..." autoFocus
              style={{ flex: 1, border: 'none', outline: 'none', fontSize: 11, background: 'transparent' }} />
            {selected.length > 0 && (
              <button onClick={() => onChange([])} style={{ fontSize: 9, color: C.red, background: 'none', border: 'none', cursor: 'pointer', fontWeight: 700 }}>Clear</button>
            )}
          </div>
          <div style={{ overflowY: 'auto', maxHeight: 300, padding: '2px 0' }}>
            {groups.map(g => {
              const state    = groupState(g.members)
              const isMulti  = g.members.length > 1
              const expanded = expandedBases.has(g.base)
              return (
                <div key={g.base}>
                  <div
                    onClick={() => toggleGroup(g.members)}
                    style={{
                      padding: '4px 10px', cursor: 'pointer', fontSize: 11,
                      display: 'flex', alignItems: 'center', gap: 6,
                      background: state === 'all' ? C.primaryLight : '#fff',
                    }}
                  >
                    <CheckBox state={state} />
                    <span style={{ flex: 1, fontWeight: isMulti ? 700 : 500 }}>{g.base}</span>
                    {isMulti && (
                      <>
                        <span style={{
                          fontSize: 9, fontWeight: 700,
                          padding: '1px 5px', borderRadius: 8,
                          background: '#f1f5f9', color: C.textMuted,
                        }}>×{g.members.length}</span>
                        <button
                          onClick={(e) => { e.stopPropagation(); toggleExpand(g.base) }}
                          title={expanded ? 'Collapse' : 'Expand presets'}
                          style={{
                            background: 'none', border: 'none', cursor: 'pointer',
                            padding: 0, color: C.textMuted,
                            transform: expanded ? 'rotate(180deg)' : 'none',
                            transition: 'transform 120ms',
                          }}
                        >
                          <ChevronDown size={10} />
                        </button>
                      </>
                    )}
                  </div>
                  {isMulti && expanded && g.members.map(c => (
                    <div
                      key={c}
                      onClick={() => toggleMember(c)}
                      style={{
                        padding: '3px 10px 3px 26px', cursor: 'pointer', fontSize: 10,
                        display: 'flex', alignItems: 'center', gap: 6,
                        background: selectedSet.has(c) ? C.primaryLight : '#fafbfc',
                        color: C.textSub,
                      }}
                    >
                      <CheckBox state={selectedSet.has(c) ? 'all' : 'none'} />
                      <span>{c.split('|', 2)[1] || c}</span>
                    </div>
                  ))}
                </div>
              )
            })}
            {groups.length === 0 && (
              <div style={{ padding: 8, textAlign: 'center', color: C.textMuted, fontSize: 11 }}>
                No matching columns
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}


function Dropdown({ label, value, options, onChange, width = 200, disabled }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const ref = useRef(null)
  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])
  const sel = options.find(o => o.value === value)
  const filtered = options.filter(o => (o.label || '').toLowerCase().includes(q.toLowerCase()))
  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button onClick={() => !disabled && setOpen(!open)} disabled={disabled} style={{
        display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px', borderRadius: 6,
        border: `1px solid ${value ? C.primary : C.cardBorder}`,
        background: disabled ? C.grayBg : (value ? C.primaryLight : '#fff'),
        cursor: disabled ? 'not-allowed' : 'pointer', fontSize: 12,
        minWidth: width, justifyContent: 'space-between',
        color: value ? C.primary : C.text,
      }}>
        <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: width - 30 }}>
          {label}: <strong>{sel?.label || 'All'}</strong>
        </span>
        <ChevronDown size={12} />
      </button>
      {open && (
        <div style={{
          position: 'absolute', top: '100%', left: 0, marginTop: 4, zIndex: 100,
          background: '#fff', border: `1px solid ${C.cardBorder}`, borderRadius: 6,
          boxShadow: '0 8px 24px rgba(0,0,0,.12)', minWidth: width, maxHeight: 360, display: 'flex', flexDirection: 'column',
        }}>
          {options.length > 8 && (
            <div style={{ padding: 6, borderBottom: `1px solid ${C.cardBorder}` }}>
              <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter…" autoFocus
                style={{ width: '100%', fontSize: 12, padding: '4px 6px', border: `1px solid ${C.inputBorder}`, borderRadius: 4 }} />
            </div>
          )}
          <div style={{ overflowY: 'auto', maxHeight: 300 }}>
            <div onClick={() => { onChange(''); setOpen(false); setQ('') }}
              style={{ padding: '6px 10px', cursor: 'pointer', fontSize: 12, color: C.textMuted, fontStyle: 'italic',
                       background: !value ? C.primaryLight : '#fff' }}>(All)</div>
            {filtered.map(o => (
              <div key={o.value} onClick={() => { onChange(o.value); setOpen(false); setQ('') }} style={{
                padding: '6px 10px', cursor: 'pointer', fontSize: 12,
                background: value === o.value ? C.primaryLight : '#fff',
                color: value === o.value ? C.primary : C.text,
              }}>{o.label}</div>
            ))}
            {filtered.length === 0 && <div style={{ padding: 8, color: C.textMuted, fontSize: 12 }}>No matches</div>}
          </div>
        </div>
      )}
    </div>
  )
}


export default function ContribReportPage() {
  const [tables, setTables] = useState([])
  // Three cascading filters that together pinpoint one Cont_Percentage_* table.
  // Default date = latest execution (most-recent table). Level + hier are picked
  // from whatever exists for the selected date.
  const [selectedDate, setSelectedDate] = useState('')
  const [selectedLevel, setSelectedLevel] = useState('')
  const [selectedHier, setSelectedHier] = useState('')

  // Per-column multi-select header-filters live in `byCol` keyed by the column
  // name (so the grouping column — e.g. M_YARN_02 — is filterable without
  // hard-coding). `q` stays a single text search on the vendor name column.
  const [filters, setFilters] = useState({ byCol: {}, q: '' })
  // Explicit column-picker selection. [] = use backend default (curated list).
  // Populated from the first response so the picker starts pre-checked with
  // exactly what's currently shown. Reset to [] when the table changes.
  const [selectedCols, setSelectedCols] = useState([])

  const handleColFilter = useCallback((col, vals) => {
    setFilters(f => {
      const next = { ...f.byCol }
      if (vals && vals.length) next[col] = vals
      else delete next[col]
      return { ...f, byCol: next }
    })
  }, [])

  const activeColFilters = useMemo(
    () => Object.entries(filters.byCol).filter(([, v]) => v && v.length),
    [filters.byCol],
  )

  const [page, setPage] = useState(1)
  const PAGE_SIZE = 500

  const [data, setData] = useState({ columns: [], rows: [], total: 0, filter_options: {}, is_store: false })
  const [loading, setLoading] = useState(false)

  // Parse all tables into a {dateOnly → level → hier → tableMeta} map.
  // When multiple executions happened on the same date for the same
  // (level, hier), keep the one with the LATEST `time` so the user sees the
  // most recent snapshot.
  const tableIndex = useMemo(() => {
    const map = new Map()  // dateOnly → Map<level, Map<hier, tableMeta>>
    for (const t of tables) {
      const p = parseTable(t.table_name)
      if (!p) continue
      const meta = { ...t, ...p }
      if (!map.has(p.dateOnly)) map.set(p.dateOnly, new Map())
      const byLevel = map.get(p.dateOnly)
      if (!byLevel.has(p.level)) byLevel.set(p.level, new Map())
      const byHier = byLevel.get(p.level)
      const existing = byHier.get(p.hier)
      // Pick latest by time string (lexicographic works because HHMM is padded).
      // A new-format table (time non-empty) always wins over a legacy month-only.
      if (!existing || (p.time && (!existing.time || p.time > existing.time))) {
        byHier.set(p.hier, meta)
      }
    }
    return map
  }, [tables])

  const dateOptions  = useMemo(
    () => Array.from(tableIndex.keys()).sort((a, b) => b.localeCompare(a)),
    [tableIndex],
  )
  const levelOptions = useMemo(
    () => selectedDate ? Array.from(tableIndex.get(selectedDate)?.keys() || []) : [],
    [tableIndex, selectedDate],
  )
  const hierOptions  = useMemo(
    () => (selectedDate && selectedLevel)
      ? Array.from(tableIndex.get(selectedDate)?.get(selectedLevel)?.keys() || [])
      : [],
    [tableIndex, selectedDate, selectedLevel],
  )

  // Resolve the picked (date, level, hier) tuple to a concrete table_name.
  const tableName = useMemo(() => {
    if (!selectedDate || !selectedLevel || !selectedHier) return ''
    return tableIndex.get(selectedDate)?.get(selectedLevel)?.get(selectedHier)?.table_name || ''
  }, [tableIndex, selectedDate, selectedLevel, selectedHier])

  // Load tables once
  useEffect(() => {
    contribAPI.reportTables().then(({ data }) => {
      const list = data?.data?.tables || []
      setTables(list)
    }).catch(e => toast.error('Failed to load tables: ' + (e.message || e)))
  }, [])

  // Seed the cascade defaults whenever the table list changes.
  // Date → latest. Level → 'store' if available, else first. Hier → first.
  useEffect(() => {
    if (!dateOptions.length) return
    const d = selectedDate && dateOptions.includes(selectedDate) ? selectedDate : dateOptions[0]
    if (d !== selectedDate) setSelectedDate(d)
    const levels = Array.from(tableIndex.get(d)?.keys() || [])
    const lv = selectedLevel && levels.includes(selectedLevel)
      ? selectedLevel
      : (levels.includes('store') ? 'store' : levels[0] || '')
    if (lv !== selectedLevel) setSelectedLevel(lv)
    const hiers = Array.from(tableIndex.get(d)?.get(lv)?.keys() || [])
    const h = selectedHier && hiers.includes(selectedHier) ? selectedHier : (hiers[0] || '')
    if (h !== selectedHier) setSelectedHier(h)
  }, [dateOptions, tableIndex])  // eslint-disable-line react-hooks/exhaustive-deps

  // When the user changes Date, re-pick Level/Hier from what exists for it.
  const handleDateChange = (d) => {
    setSelectedDate(d)
    const levels = Array.from(tableIndex.get(d)?.keys() || [])
    const lv = levels.includes(selectedLevel)
      ? selectedLevel
      : (levels.includes('store') ? 'store' : levels[0] || '')
    setSelectedLevel(lv)
    const hiers = Array.from(tableIndex.get(d)?.get(lv)?.keys() || [])
    setSelectedHier(hiers.includes(selectedHier) ? selectedHier : (hiers[0] || ''))
  }
  // When the user changes Level, re-pick Hier from what exists for (Date, Level).
  const handleLevelChange = (lv) => {
    setSelectedLevel(lv)
    const hiers = Array.from(tableIndex.get(selectedDate)?.get(lv)?.keys() || [])
    setSelectedHier(hiers.includes(selectedHier) ? selectedHier : (hiers[0] || ''))
  }

  // Reset to page 1 when table or any filter changes
  useEffect(() => { setPage(1) }, [tableName, filters.byCol, filters.q, selectedCols])

  // When the user changes the active table, drop the explicit column choice so
  // the new table's curated default is shown. Otherwise stale picks from the
  // prior table (column names that may not exist) would shrink the result to
  // empty.
  useEffect(() => { setSelectedCols([]) }, [tableName])

  // Seed selectedCols with the first response's columns so the picker UI
  // starts pre-checked with the currently visible set.
  useEffect(() => {
    if (selectedCols.length === 0 && data.columns?.length) {
      setSelectedCols(data.columns)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.columns])

  const loadPage = useCallback(async () => {
    if (!tableName) return
    setLoading(true)
    try {
      const params = { table: tableName, page, page_size: PAGE_SIZE }
      // Generic per-column filter map. Backend parses the JSON and emits an
      // IN-clause for each column that exists in the table.
      const active = Object.fromEntries(
        Object.entries(filters.byCol).filter(([, v]) => v && v.length),
      )
      if (Object.keys(active).length) params.col_filters = JSON.stringify(active)
      if (filters.q) params.q = filters.q
      // Explicit column list overrides curated default. Empty list = use default.
      if (selectedCols.length > 0) params.cols = selectedCols.join(',')
      const { data: resp } = await contribAPI.reportPage(params)
      setData(resp?.data || { columns: [], rows: [], total: 0, filter_options: {}, is_store: false })
    } catch (e) {
      toast.error('Failed to load page: ' + (e.response?.data?.detail || e.message))
      setData({ columns: [], rows: [], total: 0, filter_options: {}, is_store: false })
    } finally {
      setLoading(false)
    }
  }, [tableName, page, filters, selectedCols])

  useEffect(() => { loadPage() }, [loadPage])

  const totalPages = Math.max(1, Math.ceil((data.total || 0) / PAGE_SIZE))

  const tableMeta = tableIndex.get(selectedDate)?.get(selectedLevel)?.get(selectedHier) || null

  const filterOpts = data.filter_options || {}

  return (
    <div style={{ padding: 14, background: C.pageBg, minHeight: '100vh' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: C.text, display: 'flex', alignItems: 'center', gap: 8 }}>
            <ClipboardCheck size={18} /> Contribution Report
          </h2>
          <p style={{ margin: '4px 0 0', color: C.textMuted, fontSize: 12 }}>
            Full-table view · curated columns · paginated 500 rows / page
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={loadPage} disabled={loading} style={btn('ghost')}>
            <RefreshCw size={13} /> Refresh
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center',
                    padding: 10, background: '#fff', border: `1px solid ${C.cardBorder}`, borderRadius: 6 }}>
        <Dropdown
          label="Date"
          value={selectedDate}
          width={210}
          options={dateOptions.map(d => ({ value: d, label: dateLabel(d) }))}
          onChange={handleDateChange}
        />
        <Dropdown
          label="Level"
          value={selectedLevel}
          width={130}
          options={levelOptions.map(l => ({
            value: l,
            label: l === 'company' ? 'Company' : 'Store',
          }))}
          onChange={handleLevelChange}
        />
        <Dropdown
          label="Hierarchy"
          value={selectedHier}
          width={210}
          options={hierOptions.map(h => ({ value: h, label: hierLabel(h) }))}
          onChange={setSelectedHier}
        />
        <div style={{ position: 'relative' }}>
          <Search size={12} color={C.textMuted}
            style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)' }} />
          <input
            placeholder="Search vendor name…"
            value={filters.q}
            onChange={e => setFilters(f => ({ ...f, q: e.target.value }))}
            style={{ padding: '6px 8px 6px 26px', borderRadius: 6, border: `1px solid ${C.cardBorder}`,
                     fontSize: 12, width: 200, color: C.text, background: '#fff' }}
          />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 'auto' }}>
          <span style={{ fontSize: 11, color: C.textMuted }}>Columns:</span>
          <ColumnPicker
            allColumns={data.all_columns || []}
            selected={selectedCols}
            onChange={setSelectedCols}
          />
          <span style={{ fontSize: 11, color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>
            {selectedCols.length} / {(data.all_columns?.length ?? '?')}
          </span>
          {data.all_columns?.length > 0 && selectedCols.length !== data.all_columns.length && (
            <button
              onClick={() => setSelectedCols(data.all_columns)}
              title="Select every column in the table"
              style={{ fontSize: 10, color: C.primary, background: 'none', border: 'none',
                       cursor: 'pointer', fontWeight: 700, padding: '2px 4px' }}
            >All</button>
          )}
        </div>
      </div>

      {/* Active filter chips — one per column with selected values */}
      {activeColFilters.length > 0 && (
        <div style={{
          padding: '6px 10px', background: '#fefce8', border: `1px solid ${C.cardBorder}`,
          borderRadius: 6, marginBottom: 8, display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center',
        }}>
          <Filter size={11} color={C.amber} />
          <span style={{ fontSize: 10, fontWeight: 700, color: C.amber }}>Filters:</span>
          {activeColFilters.map(([col, vals]) => (
            <span key={col} style={{
              display: 'flex', alignItems: 'center', gap: 3, padding: '2px 8px', borderRadius: 10,
              fontSize: 10, fontWeight: 600, background: C.primaryLight, color: C.primary,
              border: `1px solid ${C.primary}`,
            }}>
              {col}: {vals.length <= 2 ? vals.join(', ') : `${vals.length} selected`}
              <button onClick={() => handleColFilter(col, [])} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
                <X size={9} color={C.primary} />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* Grid */}
      <div style={{ background: '#fff', border: `1px solid ${C.cardBorder}`, borderRadius: 6,
                    overflow: 'auto', maxHeight: '70vh' }}>
        {loading ? (
          <div style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>Loading…</div>
        ) : !tableName ? (
          <div style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>Pick a result table to begin.</div>
        ) : !data.rows.length ? (
          <div style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>
            No rows {(activeColFilters.length || filters.q)
              ? 'match the current filters.' : 'in this table.'}
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead style={{ position: 'sticky', top: 0, background: C.headerBg, zIndex: 5 }}>
              <tr>
                <th style={th}>#</th>
                {data.columns.map(c => (
                  <th key={c} style={th}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span>{c}</span>
                      {filterOpts[c] && filterOpts[c].length > 0 && (
                        <FilterDropdown
                          column={c}
                          options={filterOpts[c]}
                          selected={filters.byCol[c] || []}
                          onChange={handleColFilter}
                        />
                      )}
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r, i) => (
                <tr key={i} style={{ background: i % 2 ? C.rowAlt : '#fff' }}>
                  <td style={{ ...td, color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>
                    {(page - 1) * PAGE_SIZE + i + 1}
                  </td>
                  {data.columns.map(c => {
                    const v = r[c]
                    const isNum = typeof v === 'number'
                    return (
                      <td key={c} style={{ ...td, textAlign: isNum ? 'right' : 'left',
                                            fontVariantNumeric: 'tabular-nums',
                                            fontFamily: isNum ? 'ui-monospace, monospace' : 'inherit' }}>
                        {fmtCell(v, c)}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination footer */}
      <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11, color: C.textMuted }}>
        <span>
          {data.total > 0 ? (
            <>Showing rows <strong>{(page-1)*PAGE_SIZE+1}</strong>–<strong>{Math.min(page*PAGE_SIZE, data.total)}</strong> of <strong>{data.total.toLocaleString()}</strong> · {data.columns.length} of {data.all_columns?.length || '?'} columns</>
          ) : (
            <>Total: 0</>
          )}
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <button onClick={() => setPage(p => Math.max(1, p-1))} disabled={page <= 1 || loading} style={pageBtn(page <= 1)}>
            <ChevronLeft size={12} />
          </button>
          <span style={{ padding: '0 6px' }}>Page <strong>{page}</strong> of <strong>{totalPages}</strong></span>
          <button onClick={() => setPage(p => Math.min(totalPages, p+1))} disabled={page >= totalPages || loading} style={pageBtn(page >= totalPages)}>
            <ChevronRight size={12} />
          </button>
        </span>
      </div>

      {tableMeta && (
        <div style={{ marginTop: 4, fontSize: 10, color: C.textMuted }}>
          {tableMeta.level} table · vendor_col=<code>{tableMeta.vendor_col || '—'}</code> · total rows in table: {tableMeta.rows.toLocaleString()}
        </div>
      )}
    </div>
  )
}

const th = {
  padding: '6px 8px', borderBottom: `1px solid ${C.cardBorder}`, borderRight: `1px solid ${C.cardBorder}`,
  fontSize: 10, fontWeight: 700, color: C.text, textAlign: 'left', whiteSpace: 'nowrap', background: C.headerBg,
}
const td = {
  padding: '4px 8px', borderBottom: `1px solid ${C.cardBorder}`, borderRight: `1px solid ${C.cardBorder}`,
  whiteSpace: 'nowrap', color: C.text,
}
const btn = (variant) => ({
  display: 'flex', alignItems: 'center', gap: 6, padding: '6px 12px', borderRadius: 6,
  border: variant === 'primary' ? 'none' : `1px solid ${C.cardBorder}`,
  background: variant === 'primary' ? C.primary : '#fff',
  color: variant === 'primary' ? '#fff' : C.text,
  cursor: 'pointer', fontSize: 12, fontWeight: 600,
})
const pageBtn = (disabled) => ({
  display: 'inline-flex', alignItems: 'center', padding: '4px 8px',
  border: `1px solid ${C.cardBorder}`, background: disabled ? C.grayBg : '#fff',
  color: disabled ? C.textMuted : C.text,
  borderRadius: 4, cursor: disabled ? 'not-allowed' : 'pointer',
})
