/**
 * PendingAllocationPage — ARS_PEND_ALC overview
 * Shows approved-but-not-yet-DO'd quantities from the ARS allocation system.
 * Deducted from MSA available stock to prevent double allocation.
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { RefreshCw, Package, ChevronUp, ChevronDown, Search, X } from 'lucide-react'
import DataGrid from '@/components/DataGrid'

const C = {
  primary: '#4f46e5', blue: '#0891b2', green: '#16a34a',
  amber: '#d97706', red: '#dc2626', text: '#1e293b',
  textSub: '#64748b', textMuted: '#94a3b8', border: '#e2e8f0',
  bg: '#f8fafc', card: '#ffffff',
}

function Tile({ label, value, sub, accent }) {
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                  padding: '12px 16px', borderLeft: `3px solid ${accent}` }}>
      <div style={{ fontSize: 9, fontWeight: 700, color: C.textSub,
                    letterSpacing: '.06em', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 800, color: C.text,
                    lineHeight: 1 }}>{value ?? '—'}</div>
      {sub && <div style={{ fontSize: 9, color: C.textMuted, marginTop: 3 }}>{sub}</div>}
    </div>
  )
}

function fmt(n) { return typeof n === 'number' ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : '—' }

const MODE_COLOR = { AUTO: C.primary, MANUAL: C.amber }
const SRC_COLOR  = { AUTO: C.blue,   MANUAL: C.amber }

function ModeBadge({ value }) {
  const color = MODE_COLOR[value] || C.textSub
  return (
    <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
                   background: color + '22', color }}>
      {value || 'AUTO'}
    </span>
  )
}

/**
 * SummaryTable — small client-side sortable + searchable aggregation table.
 * Used for the by_majcat / by_mode / by_source breakdown tabs.
 *
 * Props:
 *   rows: array of objects (already aggregated)
 *   columns: [{ key, label, align, render?(row), searchable? }]
 *   defaultSort: column key to sort by initially
 *   defaultDir: 'asc' | 'desc'
 *   searchKey: column key to apply the search box against
 *   searchPlaceholder: label for the search input
 */
function SummaryTable({
  rows, columns, defaultSort, defaultDir = 'desc',
  searchKey, searchPlaceholder = 'Search…',
}) {
  const [sortBy, setSortBy]   = useState(defaultSort)
  const [sortDir, setSortDir] = useState(defaultDir)
  const [search, setSearch]   = useState('')

  const cycleSort = (key) => {
    if (sortBy !== key) { setSortBy(key); setSortDir('asc'); return }
    if (sortDir === 'asc') { setSortDir('desc'); return }
    setSortBy(null); setSortDir('desc')
  }

  const filtered = useMemo(() => {
    let arr = rows || []
    if (search && searchKey) {
      const q = search.toLowerCase()
      arr = arr.filter(r => String(r[searchKey] ?? '').toLowerCase().includes(q))
    }
    if (sortBy) {
      arr = [...arr].sort((a, b) => {
        const av = a[sortBy], bv = b[sortBy]
        const an = (av == null) ? -Infinity : (typeof av === 'number' ? av : String(av))
        const bn = (bv == null) ? -Infinity : (typeof bv === 'number' ? bv : String(bv))
        if (an < bn) return sortDir === 'asc' ? -1 : 1
        if (an > bn) return sortDir === 'asc' ? 1 : -1
        return 0
      })
    }
    return arr
  }, [rows, search, searchKey, sortBy, sortDir])

  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`,
                  borderRadius: 8, overflow: 'hidden' }}>
      {searchKey && (
        <div style={{ padding: '6px 10px', borderBottom: `1px solid ${C.border}`,
                      background: C.bg, display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ position: 'relative', flex: 1 }}>
            <Search size={11} style={{ position: 'absolute', left: 8, top: 6, color: C.textMuted }}/>
            <input value={search} onChange={e => setSearch(e.target.value)}
              placeholder={searchPlaceholder}
              style={{ width: '100%', fontSize: 10, padding: '4px 8px 4px 24px',
                       borderRadius: 3, border: `1px solid ${C.border}`,
                       outline: 'none', boxSizing: 'border-box' }}/>
            {search && (
              <X size={10} onClick={() => setSearch('')}
                style={{ position: 'absolute', right: 8, top: 7, color: C.textMuted, cursor: 'pointer' }}/>
            )}
          </div>
          <div style={{ fontSize: 9, color: C.textMuted }}>
            {filtered.length} / {(rows || []).length}
          </div>
        </div>
      )}
      <div style={{ overflowX: 'auto', maxHeight: '60vh' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
          <thead style={{ position: 'sticky', top: 0, zIndex: 1 }}>
            <tr style={{ background: C.bg }}>
              {columns.map(col => {
                const active = sortBy === col.key
                return (
                  <th key={col.key}
                    onClick={() => cycleSort(col.key)}
                    style={{ padding: '7px 10px',
                             textAlign: col.align || 'left',
                             fontSize: 9, fontWeight: 700,
                             color: active ? C.primary : C.textSub,
                             letterSpacing: '.05em',
                             borderBottom: `1px solid ${C.border}`,
                             whiteSpace: 'nowrap', cursor: 'pointer',
                             userSelect: 'none', background: C.bg }}>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                      {col.label}
                      {active && (sortDir === 'asc'
                        ? <ChevronUp size={10}/>
                        : <ChevronDown size={10}/>)}
                    </span>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr><td colSpan={columns.length}
                style={{ padding: 30, textAlign: 'center', color: C.textMuted }}>
                {(rows || []).length === 0 ? 'No data.' : 'No rows match the search'}
              </td></tr>
            ) : filtered.map((r, i) => (
              <tr key={i} style={{ borderBottom: `1px solid ${C.border}`,
                                    background: i % 2 === 0 ? '#fff' : C.bg }}>
                {columns.map(col => (
                  <td key={col.key} style={{
                    padding: '6px 10px', textAlign: col.align || 'left',
                  }}>
                    {col.render ? col.render(r) : (r[col.key] ?? '—')}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function PendingAllocationPage() {
  const [summary, setSummary]     = useState(null)
  const [loading, setLoading]     = useState(false)
  const [tab, setTab]             = useState('majcat') // 'majcat' | 'mode' | 'detail'
  const [detail, setDetail]       = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [sessionFilter, setSessionFilter] = useState('')
  const [majCatFilter, setMajCatFilter]   = useState('')
  const [modeFilter, setModeFilter]       = useState('')
  const [showClosed, setShowClosed]       = useState(false)

  const loadSummary = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await pendAlcAPI.summary()
      setSummary(data?.data || null)
    } catch {
      toast.error('Failed to load pending allocation summary')
    } finally {
      setLoading(false)
    }
  }, [])

  // DataGrid drives its own pagination/sort/filter; this fetcher just merges
  // in the top-bar filters (session/majcat/mode/closed).
  const fetchDetail = useCallback(async (gridParams) => {
    const params = { ...gridParams }
    if (sessionFilter) params.session_id  = sessionFilter
    if (majCatFilter)  params.maj_cat     = majCatFilter
    if (modeFilter)    params.alloc_mode  = modeFilter
    if (!showClosed)   params.closed      = false
    return pendAlcAPI.detail(params)
  }, [sessionFilter, majCatFilter, modeFilter, showClosed])

  const detailRefreshKey = useMemo(
    () => `${sessionFilter}|${majCatFilter}|${modeFilter}|${showClosed}`,
    [sessionFilter, majCatFilter, modeFilter, showClosed]
  )

  const [gridBumpKey, setGridBumpKey] = useState(0)

  useEffect(() => { loadSummary() }, [loadSummary])

  const t = summary?.totals

  const _btn = (active) => ({
    fontSize: 10, fontWeight: active ? 700 : 400, padding: '4px 12px',
    borderRadius: 4, border: `1px solid ${active ? C.primary : C.border}`,
    background: active ? C.primary : 'transparent',
    color: active ? '#fff' : C.textSub, cursor: 'pointer',
  })

  const TH = ({ children, right }) => (
    <th style={{ padding: '7px 10px', textAlign: right ? 'right' : 'left',
                 fontSize: 9, fontWeight: 700, color: C.textSub,
                 letterSpacing: '.05em', borderBottom: `1px solid ${C.border}`,
                 whiteSpace: 'nowrap' }}>
      {children}
    </th>
  )

  return (
    <div style={{ padding: '16px 20px', fontFamily: 'Inter,system-ui,sans-serif',
                  fontSize: 11, color: C.text, background: C.bg, minHeight: '100vh' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <Package size={16} color={C.primary}/>
        <div style={{ fontSize: 13, fontWeight: 800, color: C.text }}>Pending Allocation</div>
        <div style={{ fontSize: 10, color: C.textMuted, marginLeft: 4 }}>
          Approved quantities awaiting SAP Delivery Order
        </div>
        <div style={{ flex: 1 }}/>
        <button onClick={loadSummary} disabled={loading}
          style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 10,
                   padding: '4px 10px', borderRadius: 4, border: `1px solid ${C.border}`,
                   background: '#fff', cursor: 'pointer', color: C.textSub }}>
          <RefreshCw size={11} style={{ animation: loading ? 'spin 1s linear infinite' : 'none' }}/>
          Refresh
        </button>
      </div>

      {/* Tiles */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6,1fr)', gap: 8, marginBottom: 14 }}>
        <Tile label="TOTAL ALLOC QTY"  value={fmt(t?.total_alloc)}  accent={C.primary}/>
        <Tile label="BDC QTY (SENT)"   value={fmt(t?.total_bdc)}    accent={C.blue}
              sub={t?.pct_bdc_covered != null ? `${t.pct_bdc_covered}% DO covered` : undefined}/>
        <Tile label="DO QTY (ISSUED)"  value={fmt(t?.total_do)}     accent={C.green}/>
        <Tile label="PENDING QTY"      value={fmt(t?.total_pend)}   accent={C.amber}
              sub={t?.total_alloc ? `${(100*t.total_pend/t.total_alloc).toFixed(1)}% of alloc` : undefined}/>
        <Tile label="OPEN ROWS"        value={fmt(t?.open_rows)}    accent={C.blue}/>
        <Tile label="CLOSED %"         value={t?.pct_closed != null ? `${t.pct_closed}%` : '—'}
              accent={C.textMuted} sub={`${fmt(t?.closed_rows)} rows fully DO'd`}/>
      </div>

      {/* Tabs */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
        {[['majcat','By MAJ_CAT'],['mode','By Mode/Source'],['detail','Row Detail']].map(([k,l]) => (
          <button key={k} style={_btn(tab === k)} onClick={() => setTab(k)}>{l}</button>
        ))}
      </div>

      {/* MAJ_CAT breakdown */}
      {tab === 'majcat' && (
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, overflow: 'hidden' }}>
          {loading ? (
            <div style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>Loading…</div>
          ) : !summary?.by_majcat?.length ? (
            <div style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>No open pending quantities.</div>
          ) : (
            <SummaryTable
              rows={summary.by_majcat}
              defaultSort="pend_qty" defaultDir="desc"
              searchKey="maj_cat" searchPlaceholder="Search MAJ_CAT…"
              columns={[
                { key:'maj_cat',  label:'MAJ_CAT',
                  render:r => <span style={{fontWeight:600}}>{r.maj_cat || '—'}</span> },
                { key:'alloc_qty', label:'ALLOC QTY', align:'right',
                  render:r => fmt(r.alloc_qty) },
                { key:'bdc_qty', label:'BDC QTY', align:'right',
                  render:r => <span style={{color:C.blue}}>{fmt(r.bdc_qty)}</span> },
                { key:'do_qty', label:'DO QTY', align:'right',
                  render:r => <span style={{color:C.green}}>{fmt(r.do_qty)}</span> },
                { key:'pend_qty', label:'PEND QTY', align:'right',
                  render:r => <span style={{fontWeight:700, color:r.pend_qty>0?C.amber:C.green}}>
                    {fmt(r.pend_qty)}</span> },
                { key:'rows', label:'ROWS', align:'right',
                  render:r => <span style={{color:C.textMuted}}>{r.rows}</span> },
              ]}
            />
          )}
        </div>
      )}

      {/* Mode / Source breakdown */}
      {tab === 'mode' && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          {/* By Mode */}
          <div>
            <div style={{ padding: '8px 12px', fontSize: 10, fontWeight: 700,
                          color: C.textSub, letterSpacing: '.05em', marginBottom: 4 }}>
              BY ALLOC_MODE
            </div>
            {loading ? (
              <div style={{ padding: 30, textAlign: 'center', color: C.textMuted,
                            background: C.card, border: `1px solid ${C.border}`,
                            borderRadius: 8 }}>Loading…</div>
            ) : (
              <SummaryTable
                rows={summary?.by_mode}
                defaultSort="pend_qty" defaultDir="desc"
                searchKey="mode" searchPlaceholder="Search mode…"
                columns={[
                  { key:'mode', label:'MODE',
                    render:r => <ModeBadge value={r.mode}/> },
                  { key:'alloc_qty', label:'ALLOC', align:'right',
                    render:r => fmt(r.alloc_qty) },
                  { key:'bdc_qty', label:'BDC', align:'right',
                    render:r => <span style={{color:C.blue}}>{fmt(r.bdc_qty)}</span> },
                  { key:'do_qty', label:'DO', align:'right',
                    render:r => <span style={{color:C.green}}>{fmt(r.do_qty)}</span> },
                  { key:'pend_qty', label:'PEND', align:'right',
                    render:r => <span style={{fontWeight:700, color:r.pend_qty>0?C.amber:C.green}}>
                      {fmt(r.pend_qty)}</span> },
                  { key:'rows', label:'ROWS', align:'right',
                    render:r => <span style={{color:C.textMuted}}>{r.rows}</span> },
                ]}
              />
            )}
          </div>
          {/* By Source */}
          <div>
            <div style={{ padding: '8px 12px', fontSize: 10, fontWeight: 700,
                          color: C.textSub, letterSpacing: '.05em', marginBottom: 4 }}>
              BY SOURCE
            </div>
            {loading ? (
              <div style={{ padding: 30, textAlign: 'center', color: C.textMuted,
                            background: C.card, border: `1px solid ${C.border}`,
                            borderRadius: 8 }}>Loading…</div>
            ) : (
              <SummaryTable
                rows={summary?.by_source}
                defaultSort="pend_qty" defaultDir="desc"
                searchKey="source" searchPlaceholder="Search source…"
                columns={[
                  { key:'source', label:'SOURCE',
                    render:r => <span style={{fontSize:8, fontWeight:700, padding:'2px 6px',
                                                borderRadius:3,
                                                background:(SRC_COLOR[r.source]||C.textSub)+'22',
                                                color:SRC_COLOR[r.source]||C.textSub}}>
                      {r.source || 'AUTO'}</span> },
                  { key:'alloc_qty', label:'ALLOC', align:'right',
                    render:r => fmt(r.alloc_qty) },
                  { key:'bdc_qty', label:'BDC', align:'right',
                    render:r => <span style={{color:C.blue}}>{fmt(r.bdc_qty)}</span> },
                  { key:'do_qty', label:'DO', align:'right',
                    render:r => <span style={{color:C.green}}>{fmt(r.do_qty)}</span> },
                  { key:'pend_qty', label:'PEND', align:'right',
                    render:r => <span style={{fontWeight:700, color:r.pend_qty>0?C.amber:C.green}}>
                      {fmt(r.pend_qty)}</span> },
                  { key:'rows', label:'ROWS', align:'right',
                    render:r => <span style={{color:C.textMuted}}>{r.rows}</span> },
                ]}
              />
            )}
          </div>
        </div>
      )}

      {/* Detail rows */}
      {tab === 'detail' && (
        <>
          <div style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <input value={sessionFilter} onChange={e => setSessionFilter(e.target.value)}
              placeholder="Session ID…"
              style={{ fontSize: 10, padding: '4px 8px', border: `1px solid ${C.border}`,
                       borderRadius: 4, width: 180 }}/>
            <input value={majCatFilter} onChange={e => setMajCatFilter(e.target.value)}
              placeholder="MAJ_CAT…"
              style={{ fontSize: 10, padding: '4px 8px', border: `1px solid ${C.border}`,
                       borderRadius: 4, width: 120 }}/>
            <select value={modeFilter} onChange={e => setModeFilter(e.target.value)}
              style={{ fontSize: 10, padding: '4px 8px', border: `1px solid ${C.border}`,
                       borderRadius: 4 }}>
              <option value="">All Modes</option>
              <option value="AUTO">AUTO</option>
              <option value="MANUAL">MANUAL</option>
            </select>
            <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 10, cursor: 'pointer' }}>
              <input type="checkbox" checked={showClosed} onChange={e => setShowClosed(e.target.checked)}
                style={{ accentColor: C.primary }}/>
              Include closed
            </label>
            <button onClick={() => setGridBumpKey(k => k + 1)}
              style={{ ..._btn(false), display: 'flex', alignItems: 'center', gap: 4 }}>
              <RefreshCw size={10}/> Apply
            </button>
          </div>
          <DataGrid
            fetcher={fetchDetail}
            refreshKey={`${detailRefreshKey}|${gridBumpKey}|${tab}`}
            defaultPageSize={100}
            defaultSortBy="approved_at"
            defaultSortDir="desc"
            compact
            emptyText="No rows match the current filters."
            columns={[
              { key:'session_id', label:'SESSION', sortable:true,
                render:r => <span style={{fontFamily:'monospace', fontSize:9, color:C.textSub,
                                            display:'inline-block', maxWidth:110, overflow:'hidden',
                                            textOverflow:'ellipsis', whiteSpace:'nowrap',
                                            verticalAlign:'middle'}}>{r.session_id}</span> },
              { key:'rdc', label:'RDC', sortable:true, filterType:'multi',
                filterOptions:['DH24','DH26','DW01'],
                render:r => <span style={{fontWeight:600}}>{r.rdc}</span> },
              { key:'st_cd', label:'ST_CD', sortable:true, filterType:'text',
                render:r => <span style={{color:C.textSub}}>{r.st_cd || '—'}</span> },
              { key:'article_number', label:'ARTICLE', sortable:true, filterType:'text',
                render:r => <span style={{fontFamily:'monospace', fontSize:9}}>{r.article_number}</span> },
              { key:'maj_cat', label:'MAJ_CAT', sortable:true, filterType:'text',
                render:r => r.maj_cat || '—' },
              { key:'alloc_mode', label:'MODE', sortable:true, filterType:'multi',
                filterOptions:['AUTO','MANUAL','RL','TBL','NL'],
                render:r => <ModeBadge value={r.alloc_mode}/> },
              { key:'source', label:'SOURCE', sortable:true, filterType:'multi',
                filterOptions:['AUTO','MANUAL'],
                render:r => <span style={{fontSize:8, fontWeight:700, padding:'2px 6px', borderRadius:3,
                                            background:(SRC_COLOR[r.source]||C.textSub)+'22',
                                            color:SRC_COLOR[r.source]||C.textSub}}>{r.source || 'AUTO'}</span> },
              { key:'alloc_qty', label:'ALLOC', sortable:true, align:'right',
                render:r => fmt(r.alloc_qty) },
              { key:'bdc_qty', label:'BDC', sortable:true, align:'right',
                render:r => <span style={{color:C.blue}}>{fmt(r.bdc_qty)}</span> },
              { key:'do_qty', label:'DO', sortable:true, align:'right',
                render:r => <span style={{color:C.green}}>{fmt(r.do_qty)}</span> },
              { key:'pend_qty', label:'PEND', sortable:true, align:'right',
                render:r => <span style={{fontWeight:700, color:r.pend_qty>0?C.amber:C.green}}>
                  {fmt(r.pend_qty)}</span> },
              { key:'bdc_alloc_no', label:'BDC ALLOC #', sortable:true, filterType:'text',
                render:r => r.bdc_alloc_no
                  ? <span style={{fontFamily:'monospace', fontSize:9, color:C.primary}}>{r.bdc_alloc_no}</span>
                  : <span style={{color:C.textMuted}}>—</span> },
              { key:'bdc_status', label:'BDC STATUS', sortable:true, filterType:'multi',
                filterOptions:['NEVER_SENT','OPEN','PARTIAL','CONFIRMED'],
                render:r => {
                  if (!r.bdc_status) return <span style={{color:C.textMuted}}>—</span>
                  const map = {
                    NEVER_SENT:{bg:'#f1f5f9', fg:C.textMuted, label:'NEVER SENT'},
                    OPEN:      {bg:'#fef3c7', fg:C.amber,     label:'OPEN'},
                    PARTIAL:   {bg:'#dbeafe', fg:C.blue,      label:'PARTIAL'},
                    CONFIRMED: {bg:'#dcfce7', fg:C.green,     label:'CONFIRMED'},
                  }
                  const s = map[r.bdc_status] || {bg:'#f1f5f9', fg:C.textSub, label:r.bdc_status}
                  return <span style={{fontSize:8, fontWeight:700, padding:'2px 6px', borderRadius:3,
                                        background:s.bg, color:s.fg}}>{s.label}</span>
                }},
              { key:'do_received', label:'DO RECVD', sortable:true, align:'right',
                render:r => r.do_received != null
                  ? <span style={{color:C.green}}>{fmt(r.do_received)}</span>
                  : <span style={{color:C.textMuted}}>—</span> },
              { key:'do_number', label:'DO NUMBER', sortable:true,
                render:r => <span style={{fontFamily:'monospace', fontSize:9, color:C.textSub}}>
                  {r.do_number || '—'}</span> },
              { key:'approved_at', label:'APPROVED', sortable:true,
                render:r => <span style={{fontSize:9, color:C.textMuted}}>
                  {r.approved_at ? r.approved_at.slice(0,16).replace('T',' ') : '—'}</span> },
              { key:'is_closed', label:'STATUS', sortable:true,
                render:r => <span style={{fontSize:8, fontWeight:700, padding:'2px 6px', borderRadius:3,
                                            background:r.is_closed?'#dcfce7':'#fef3c7',
                                            color:r.is_closed?C.green:C.amber}}>
                  {r.is_closed ? 'CLOSED' : 'OPEN'}</span> },
            ]}
          />
        </>
      )}

      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}
