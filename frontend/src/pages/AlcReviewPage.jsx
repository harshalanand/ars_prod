/**
 * AlcReviewPage — Allocation Review (session-wise, parked or history).
 *
 * Session-by-session deep review of listing + allocation outputs from the
 * PARKED / HISTORY archives. Never reads from ARS_PEND_ALC — those sessions
 * lack the rich MBQ / STOCK / STORE_STK / REQ snapshot needed for the
 * reference report.
 *
 * Layout:
 *   ┌──────────────────────────────────────────────────────────────────┐
 *   │  Header + filters (date range, source, search)                    │
 *   ├──────────────┬──────────────────────────────────────────────────┤
 *   │  Sessions    │  Right pane: rich MAJ_CAT × RDC pivot for picked  │
 *   │  grouped     │  session + drill (SEG → DIV → SUB_DIV → MAJ_CAT   │
 *   │  by day      │  → Store → GEN_ART → Article).                    │
 *   └──────────────┴──────────────────────────────────────────────────┘
 */
import { useState, useEffect, useMemo, useCallback, useRef, Fragment } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  History, RefreshCw, Loader2, ChevronRight, ChevronDown, ChevronLeft, Search,
  ClipboardList, Archive, Clock, PanelLeftClose, PanelLeftOpen,
} from 'lucide-react'
import { arsDashboardAPI } from '../services/api'
import {
  SessionReviewGrid, FlatDrillTable,
  DRILL_LEVELS_MJST, DRILL_LEVELS_STMJ, DRILL_LABELS, DRILL_CRUMB_KEY,
} from './ArsDashboardPage'

const fmt = (n) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString('en-IN')

/* ─────────────────────────────────────────────────────────────────────────
   Date helpers — work in user's local timezone so groups match the wall clock.
───────────────────────────────────────────────────────────────────────── */
const isoDate = (d) => {
  // Returns YYYY-MM-DD in local time
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
const today = () => isoDate(new Date())
const minusDays = (n) => {
  const d = new Date(); d.setDate(d.getDate() - n); return isoDate(d)
}
const fmtDayLabel = (iso) => {
  // "Thu, 28 May 2026"
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  return d.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' })
}
const fmtTimeShort = (isoTs) => {
  if (!isoTs) return ''
  const d = new Date(isoTs)
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })
}
const dateOfTs = (isoTs) => {
  if (!isoTs) return ''
  return isoTs.slice(0, 10)
}

/* ─────────────────────────────────────────────────────────────────────────
   Right pane: drill-aware view for ONE picked session.
   - Reuses the same 7-level drill as ARS Dashboard's Product Drill tab.
   - At MAJ_CAT level renders the rich SessionReviewGrid (PARKED/HISTORY).
   - All other levels use FlatDrillTable (sortable + per-column filterable).
───────────────────────────────────────────────────────────────────────── */
// Levels above MAJ_CAT (SEG/DIV/SUB_DIV) only — used by the quick-jump button.
const QUICK_LEVELS_MJST = ['MAJ_CAT', 'ST_CD', 'GEN_ART', 'ARTICLE']
const QUICK_LEVELS_STMJ = ['ST_CD', 'GEN_ART', 'ARTICLE']

// Grid-dim review — every sec-cap grid available on the listing archive.
// Each is a column with matching *_MBQ / *_STK_TTL / *_REQ rollups, so the
// fill-rate aggregation is meaningful. Some may be NULL in particular
// sessions (MACRO_MVGR / MICRO_MVGR depend on upstream feed).
const GRID_DIMS = [
  'FAB', 'RNG_SEG', 'MERGE_RNG_SEG', 'M_VND_CD',
  'M_YARN_02', 'WEAVE_2', 'CLR',
  'MACRO_MVGR', 'MICRO_MVGR',
]
const GRID_LABELS = {
  FAB:           'Fabric',
  RNG_SEG:       'Range Seg',
  MERGE_RNG_SEG: 'Merge Range Seg',
  M_VND_CD:      'Vendor',
  M_YARN_02:     'Yarn',
  WEAVE_2:       'Weave',
  CLR:           'Colour',
  MACRO_MVGR:    'Macro MVGR',
  MICRO_MVGR:    'Micro MVGR',
}

function SessionDrillView({ sid }) {
  const [drillPath, setDrillPath] = useState('mjst')
  // Quick view skips the upper rollups (SEG/DIV/SUB_DIV) and starts at MAJ_CAT
  // (in mjst) or Store (in stmj).
  const [quickView, setQuickView] = useState(false)
  // Toggle for the optional RDC pivot at SEG/DIV/SUB_DIV/MAJ_CAT in mjst direction.
  // Default off — TOTAL columns first, expand to RDCs on demand.
  const [rdcOpen, setRdcOpen] = useState(false)
  // Grid-dim review override. When set (e.g. 'FAB'), the report groups by that
  // sec-cap grid dimension instead of the standard SEG/MAJ_CAT path. Clearing
  // (set to '') returns to the standard drill.
  const [gridDim, setGridDim] = useState('')

  const levels = useMemo(() => {
    if (quickView) return drillPath === 'stmj' ? QUICK_LEVELS_STMJ : QUICK_LEVELS_MJST
    return drillPath === 'stmj' ? DRILL_LEVELS_STMJ : DRILL_LEVELS_MJST
  }, [drillPath, quickView])

  const [crumb, setCrumb] = useState({ seg:'', div:'', sub_div:'', maj_cat:'', st_cd:'', st_nm:'', gen_art:'', clr:'' })
  const levelIdx = useMemo(() => {
    for (let i = 0; i < levels.length; i++) {
      const k = DRILL_CRUMB_KEY[levels[i]]
      if (!crumb[k]) return i
    }
    return levels.length - 1
  }, [levels, crumb])
  // gridDim is a "branch override" — it replaces what the user is currently looking
  // at WITHOUT changing the crumb scope. So if the user has drilled to MAJ_CAT/HB05
  // and applies gridDim=FAB, they see the FAB pivot scoped to HB05. Crumb intact,
  // breadcrumb still shows the standard path.
  const currentDim = gridDim || levels[levelIdx]
  const isGridBranch = !!gridDim

  const [pivot,   setPivot]   = useState({ rdcs: [], items: [], totals: {}, source: null })
  const [filter,  setFilter]  = useState('')
  const [busy,    setBusy]    = useState(false)

  // Backend session-review accepts crumb filters by these query param names.
  // (Same names work for the listing-archive aggregation and the MSA stock join.)
  const SR_PARAM_KEY = { SEG: 'seg', DIV: 'div', SUB_DIV: 'sub_div', MAJ_CAT: 'mc',
                         ST_CD: 'werks', GEN_ART: 'gen_art' }

  // Build query params from crumb (parents only — current level is the dim itself).
  const params = useMemo(() => {
    const p = { sid, dim: currentDim }
    for (let i = 0; i < levelIdx; i++) {
      const dim = levels[i]
      const v = crumb[DRILL_CRUMB_KEY[dim]]
      if (!v) continue
      const k = SR_PARAM_KEY[dim]
      if (k) p[k] = v
    }
    // GEN_ART parent also needs CLR as a separate filter
    if (crumb.gen_art && currentDim === 'ARTICLE') {
      p.clr = crumb.clr || ''
    }
    return p
  }, [sid, levels, levelIdx, currentDim, crumb])

  const crumbKey = JSON.stringify(crumb)

  // Single endpoint serves every level — rich pivot at every drill depth.
  useEffect(() => {
    if (!sid) return
    let cancel = false
    setBusy(true)
    arsDashboardAPI.sessionReview(params)
      .then(r => { if (!cancel) setPivot(r?.data?.data || { rdcs:[], items:[], totals:{}, source:null }) })
      .catch(() => {})
      .finally(() => !cancel && setBusy(false))
    return () => { cancel = true }
  }, [sid, currentDim, crumbKey])

  // Reset crumb when sid, drill direction or view mode changes.
  // gridDim does NOT reset the crumb — applying a grid view should preserve
  // the user's current scope (e.g. "FAB pivot for M_TEES_HS at HB05").
  useEffect(() => {
    setCrumb({ seg:'', div:'', sub_div:'', maj_cat:'', st_cd:'', st_nm:'', gen_art:'', clr:'' })
    setFilter('')
    setGridDim('')   // clear any grid branch when changing drill mode
  }, [sid, drillPath, quickView])

  // When the user goes back via breadcrumb to a level above where they applied
  // the grid branch, clear it (grid view no longer makes sense).
  useEffect(() => {
    setGridDim('')
    setFilter('')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [crumb.seg, crumb.div, crumb.sub_div, crumb.maj_cat, crumb.st_cd])

  // Drill into a clicked row at the current level.
  const drillInto = (row) => {
    if (currentDim === 'ARTICLE') return  // leaf
    const next = { ...crumb }
    const k = DRILL_CRUMB_KEY[currentDim]
    // The pivot row uses `maj_cat` as the generic key alias (server-side).
    const value = row.key || row.maj_cat
    if (currentDim === 'ST_CD') {
      next.st_cd = value
      next.st_nm = row.st_nm || ''
    } else if (currentDim === 'GEN_ART') {
      // value is "<gen_art> · <clr>"; server also sent gen_art_number + clr sidecar.
      next.gen_art = row.gen_art_number ? String(row.gen_art_number) : value.split(' · ')[0]
      next.clr     = row.clr != null ? row.clr : (value.split(' · ')[1] || '')
    } else {
      next[k] = value
    }
    setCrumb(next); setFilter('')
  }

  const goLevel = (idx) => {
    const next = { ...crumb }
    for (let i = idx; i < levels.length; i++) {
      const dim = levels[i]
      if (dim === 'GEN_ART')      { next.gen_art = ''; next.clr = '' }
      else if (dim === 'ST_CD')   { next.st_cd = ''; next.st_nm = '' }
      else                        next[DRILL_CRUMB_KEY[dim]] = ''
    }
    setCrumb(next); setFilter('')
  }

  const Crumb = ({ label, onClick, active }) => (
    <button onClick={onClick} className={active ? 'text-gray-900 font-semibold cursor-default' : 'text-indigo-600 hover:underline cursor-pointer'}>
      {label}
    </button>
  )

  // Store-level layout is a flat (Store × RDC) table with HUB / RDC columns,
  // and the STOCK / MSA_REM / STK% columns removed. Build the flat rows from
  // the pivot response by exploding each item's by_rdc map.
  const storeFlatRows = useMemo(() => {
    if (currentDim !== 'ST_CD') return []
    const out = []
    ;(pivot.items || []).forEach(it => {
      Object.entries(it.by_rdc || {}).forEach(([rdc, cell]) => {
        out.push({
          st_cd:      it.maj_cat || it.key,
          st_nm:      it.st_nm   || '',
          hub:        it.hub     || '',
          rdc,
          mbq:        cell.mbq        || 0,
          store_stk:  cell.store_stk  || 0,
          excess_stk: cell.excess_stk || 0,
          req:        cell.req        || 0,
          alloc:      cell.alloc      || 0,
          req_pct:    cell.req_pct    || 0,
          fill_pct:   cell.fill_pct   || 0,
          req_rem:    cell.req_rem    || 0,
          hold:       cell.hold       || 0,
        })
      })
    })
    return out
  }, [currentDim, pivot])

  const storeColumns = [
    { k: 'st_cd',      l: 'ST_CD',     cls: 'font-mono text-[11px] text-indigo-700 font-semibold' },
    { k: 'st_nm',      l: 'Store Name', cls: 'text-gray-900',
      render: (r) => r.st_nm || '—' },
    { k: 'hub',        l: 'HUB',
      render: (r) => r.hub || '—' },
    { k: 'rdc',        l: 'RDC',       cls: 'font-mono text-[11px] text-indigo-600' },
    { k: 'mbq',        l: 'MBQ',         align: 'right', fmt: true },
    { k: 'store_stk',  l: 'STORE_STK',   align: 'right', fmt: true },
    { k: 'excess_stk', l: 'EXCESS_STK',  align: 'right', fmt: true },
    { k: 'req',        l: 'REQ',         align: 'right', fmt: true },
    { k: 'alloc',      l: 'ALLOC',       align: 'right', fmt: true, cls: 'font-semibold text-gray-900' },
    { k: 'req_pct',    l: 'REQ%',        align: 'right', type: 'number',
      render: (r) => (r.req_pct ?? 0).toFixed(1) + '%' },
    { k: 'fill_pct',   l: 'FILL%',       align: 'right', type: 'number',
      render: (r) => (r.fill_pct ?? 0).toFixed(1) + '%' },
    { k: 'req_rem',    l: 'REQ_REM',     align: 'right', fmt: true, warn: true },
    { k: 'hold',       l: 'HOLD',        align: 'right', fmt: true },
  ]

  // Click a Store-level row → set st_cd crumb and advance to next level (GEN_ART).
  const storeRowClick = (row) => {
    setCrumb({ ...crumb, st_cd: row.st_cd, st_nm: row.st_nm || '' })
    setFilter('')
  }

  // OPT-level (GEN_ART · CLR) flat layout — analogous to Store-level. One row
  // per (GEN_ART · CLR, RDC). Adds OPT_TYPE / RANK / ALLOC_SEQ / STATUS /
  // REMARKS / I_ROD planned / I_ROD used / ALLOC_WAVE. Drops STOCK / MSA_REM / STK%.
  const optFlatRows = useMemo(() => {
    if (currentDim !== 'GEN_ART') return []
    const out = []
    ;(pivot.items || []).forEach(it => {
      const genArt = it.gen_art_number != null ? String(it.gen_art_number)
                    : (it.maj_cat || it.key || '').split(' · ')[0]
      const clr    = it.clr != null ? it.clr
                    : ((it.maj_cat || it.key || '').split(' · ')[1] || '')
      Object.entries(it.by_rdc || {}).forEach(([rdc, cell]) => {
        out.push({
          gen_art:    genArt,
          clr,
          label:      it.maj_cat || it.key,
          opt_type:   cell.opt_type   || '',
          rank:       cell.opt_priority_rank ?? null,
          alloc_seq:  cell.alloc_seq  ?? null,
          alloc_wave: cell.alloc_wave || '',
          status:     cell.alloc_status  || '',
          remarks:    cell.alloc_remarks || '',
          rdc,
          mbq:        cell.mbq        || 0,
          store_stk:  cell.store_stk  || 0,
          excess_stk: cell.excess_stk || 0,
          req:        cell.req        || 0,
          alloc:      cell.alloc      || 0,
          req_pct:    cell.req_pct    || 0,
          fill_pct:   cell.fill_pct   || 0,
          req_rem:    cell.req_rem    || 0,
          hold:       cell.hold       || 0,
          i_rod_planned: cell.i_rod_planned ?? 0,
          i_rod_used:    cell.i_rod_used    ?? 0,
        })
      })
    })
    return out
  }, [currentDim, pivot])

  const optColumns = [
    { k: 'label',      l: 'GEN_ART · CLR', cls: 'font-mono text-[11px] text-indigo-700 font-semibold' },
    { k: 'opt_type',   l: 'OPT_TYPE',   cls: 'text-[11px] font-medium',
      render: (r) => r.opt_type || '—' },
    { k: 'rank',       l: 'RANK',       align: 'right', type: 'number',
      render: (r) => r.rank == null ? '—' : r.rank },
    { k: 'alloc_seq',  l: 'ALLOC_SEQ',  align: 'right', type: 'number',
      render: (r) => r.alloc_seq == null ? '—' : r.alloc_seq },
    { k: 'alloc_wave', l: 'WAVE',       cls: 'font-mono text-[11px]',
      render: (r) => r.alloc_wave || '—' },
    { k: 'rdc',        l: 'RDC',        cls: 'font-mono text-[11px] text-indigo-600' },
    { k: 'i_rod_planned', l: 'I_ROD PLAN',  align: 'right', fmt: true,
      cls: 'text-[11px] text-gray-700' },
    { k: 'i_rod_used',    l: 'I_ROD USED',  align: 'right', fmt: true,
      cls: 'text-[11px] font-semibold text-gray-900' },
    { k: 'mbq',        l: 'MBQ',         align: 'right', fmt: true },
    { k: 'store_stk',  l: 'STORE_STK',   align: 'right', fmt: true },
    { k: 'excess_stk', l: 'EXCESS_STK',  align: 'right', fmt: true },
    { k: 'req',        l: 'REQ',         align: 'right', fmt: true },
    { k: 'alloc',      l: 'ALLOC',       align: 'right', fmt: true, cls: 'font-semibold text-gray-900' },
    { k: 'req_pct',    l: 'REQ%',        align: 'right', type: 'number',
      render: (r) => (r.req_pct ?? 0).toFixed(1) + '%' },
    { k: 'fill_pct',   l: 'FILL%',       align: 'right', type: 'number',
      render: (r) => (r.fill_pct ?? 0).toFixed(1) + '%' },
    { k: 'req_rem',    l: 'REQ_REM',     align: 'right', fmt: true, warn: true },
    { k: 'hold',       l: 'HOLD',        align: 'right', fmt: true },
    { k: 'status',     l: 'STATUS',      cls: 'text-[11px]',
      render: (r) => r.status || '—' },
    // REMARKS reveals on hover only — keep the column narrow with an info icon.
    { k: 'remarks',    l: '',            cls: 'text-center',
      render: (r) => r.remarks
        ? <span title={r.remarks}
                className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-indigo-50 text-indigo-600 text-[10px] cursor-help hover:bg-indigo-100">i</span>
        : '' },
  ]

  // Click an OPT-level row → drill into article level for that GEN_ART · CLR.
  const optRowClick = (row) => {
    setCrumb({ ...crumb, gen_art: row.gen_art, clr: row.clr })
    setFilter('')
  }

  // Article-level (VAR_ART × SZ) flat layout — matches the listing-page
  // reference report (VAR_ART | SZ | CONT | PAK_SZ | SZ_MBQ | SZ_STK | SZ_REQ
  // | FNL_Q | MSA_REM | SHIP | FROM_HOLD | HOLD | ALLOC | STATUS | REASON
  // | BAND_TRACE). Backend returns flat rows under `pivot.items` for this dim.
  const articleRows = useMemo(() => {
    if (currentDim !== 'ARTICLE') return []
    return (pivot.items || [])
  }, [currentDim, pivot])

  const statusCls = (s) => {
    const v = (s || '').toUpperCase()
    if (v === 'ALLOCATED')         return 'text-emerald-700 font-semibold'
    if (v === 'PARTIAL')           return 'text-amber-700 font-semibold'
    if (v === 'SKIPPED' || v === 'NOT_ALLOCATED') return 'text-rose-600'
    return 'text-gray-600'
  }

  const articleColumns = [
    { k: 'var_art',    l: 'VAR_ART',     cls: 'font-mono text-[11px] text-indigo-700 font-semibold' },
    { k: 'sz',         l: 'SZ',          cls: 'font-semibold' },
    { k: 'cont',       l: 'CONT',        align: 'right', type: 'number',
      render: (r) => (r.cont ?? 0).toFixed(3) },
    { k: 'pak_sz',     l: 'PAK_SZ',      align: 'right', type: 'number',
      render: (r) => (r.pak_sz ?? 0) },
    { k: 'sz_mbq',     l: 'SZ_MBQ',      align: 'right', fmt: true },
    { k: 'sz_stk',     l: 'SZ_STK',      align: 'right', fmt: true },
    { k: 'sz_req',     l: 'SZ_REQ',      align: 'right', fmt: true },
    { k: 'fnl_q',      l: 'FNL_Q',       align: 'right', fmt: true },
    { k: 'msa_rem',    l: 'MSA_REM',     align: 'right', fmt: true },
    { k: 'ship',       l: 'SHIP',        align: 'right', fmt: true, cls: 'font-semibold text-gray-900' },
    { k: 'from_hold',  l: 'FROM_HOLD',   align: 'right', fmt: true },
    { k: 'hold',       l: 'HOLD',        align: 'right', fmt: true },
    { k: 'alloc',      l: 'ALLOC',       align: 'right', fmt: true, cls: 'font-semibold text-gray-900' },
    { k: 'status',     l: 'STATUS',      cls: 'text-[11px]',
      render: (r) => <span className={statusCls(r.status)}>{r.status || '—'}</span> },
    { k: 'reason',     l: 'REASON',      cls: 'text-[11px] text-rose-600',
      render: (r) => r.reason || '—' },
    { k: 'band_trace', l: 'BAND_TRACE',  cls: 'font-mono text-[11px] text-gray-500',
      render: (r) => r.band_trace
        ? <span title={r.remarks || r.band_trace}
                className="cursor-help">{r.band_trace}</span>
        : '—' },
  ]

  // RDC pivot visibility rules:
  //   - drill = Store → SEG (stmj): NEVER show RDC pivot (already store-oriented).
  //   - drill = SEG → Store (mjst):
  //       • Store / GEN_ART / Article — NEVER show RDC pivot (deepest views).
  //       • SEG / DIV / SUB_DIV / MAJ_CAT — TOTAL-first, RDC optional (toggle).
  const isRollup    = currentDim === 'SEG' || currentDim === 'DIV' ||
                      currentDim === 'SUB_DIV' || currentDim === 'MAJ_CAT'
  const canShowRdc  = drillPath === 'mjst' && isRollup
  const effectiveShowRdc = canShowRdc && rdcOpen

  // Quick-jump button — visible only when the user is still in the upper rollup
  // tier (SEG/DIV/SUB_DIV in mjst) and quickView isn't already on.
  const quickJumpLabel = drillPath === 'mjst' ? 'Open MAJ_CAT' : 'Open Store'
  const showQuickJumpBtn = !quickView && (
    (drillPath === 'mjst' && (currentDim === 'SEG' || currentDim === 'DIV' || currentDim === 'SUB_DIV')) ||
    (drillPath === 'stmj' && currentDim !== 'ST_CD' && currentDim !== 'GEN_ART' && currentDim !== 'ARTICLE')
  )

  if (!sid) {
    return (
      <div className="h-72 flex items-center justify-center text-sm text-gray-400">
        Select a session from the left to begin reviewing
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {/* Single-row toolbar: breadcrumb · filter · drill toggle — all on one line */}
      <div className="flex items-center flex-wrap gap-2 px-2 py-1.5 bg-gray-50 border border-gray-200 rounded-lg">
        {/* Breadcrumb */}
        <div className="flex items-center gap-1 text-xs flex-wrap">
          {levels.map((dim, i) => {
            if (i > levelIdx) return null
            const key = DRILL_CRUMB_KEY[dim]
            const val = crumb[key]
            let label
            // When a grid branch is active, the deepest standard crumb shows
            // its picked value (not the dim label) so the user can see the
            // scope they're viewing the grid for.
            if (i === levelIdx && !isGridBranch) label = DRILL_LABELS[dim] || dim
            else if (dim === 'GEN_ART') label = val ? `${val}${crumb.clr ? ' · ' + crumb.clr : ''}` : (DRILL_LABELS[dim] || dim)
            else if (dim === 'ST_CD')   label = val ? (crumb.st_nm ? `${val} · ${crumb.st_nm}` : val) : (DRILL_LABELS[dim] || dim)
            else                        label = val || (DRILL_LABELS[dim] || dim)
            return (
              <Fragment key={dim}>
                {i > 0 && <ChevronRight size={11} className="text-gray-400" />}
                <Crumb label={label} onClick={() => goLevel(i)} active={i === levelIdx && !isGridBranch} />
              </Fragment>
            )
          })}
          {isGridBranch && (
            <>
              <ChevronRight size={11} className="text-gray-400" />
              <span className="px-1.5 py-0.5 text-[10px] font-semibold rounded bg-purple-100 text-purple-800 border border-purple-200">
                Grid: {GRID_LABELS[gridDim] || gridDim}
              </span>
              <button onClick={() => setGridDim('')}
                      title="Clear grid view"
                      className="text-[10px] text-rose-600 hover:text-rose-800 ml-0.5">✕</button>
            </>
          )}
        </div>
        {/* Filter input + level chip */}
        <div className="relative">
          <Search size={11} className="absolute left-2 top-1.5 text-gray-400" />
          <input value={filter} onChange={e => setFilter(e.target.value)}
                 placeholder={`Filter ${(DRILL_LABELS[currentDim] || currentDim).toLowerCase()}…`}
                 className="text-[11px] border border-gray-200 rounded pl-6 pr-2 py-1 bg-white w-44" />
        </div>
        <span className="text-[10px] text-gray-500 px-1 py-0.5 bg-white border border-gray-200 rounded">
          L<b>{levelIdx + 1}</b> · {DRILL_LABELS[currentDim] || currentDim}
        </span>
        {busy && <Loader2 size={12} className="animate-spin text-indigo-500" />}
        {/* Quick jump + drill direction */}
        <div className="ml-auto flex items-center gap-2">
          {(showQuickJumpBtn || quickView) && !isGridBranch && (
            <button onClick={() => setQuickView(v => !v)}
                    title={quickView ? 'Show full drill (SEG → … → Article)' : 'Skip the SEG/DIV rollups'}
                    className={`px-2 py-0.5 text-[10px] font-medium rounded border ${
                      quickView ? 'bg-amber-100 border-amber-300 text-amber-800 hover:bg-amber-200'
                                : 'bg-white border-gray-200 text-indigo-700 hover:bg-indigo-50'
                    }`}>
              {quickView ? '← Full drill' : quickJumpLabel}
            </button>
          )}
          <span className="text-[10px] text-gray-500">Drill</span>
          <div className="inline-flex border border-gray-200 rounded-md overflow-hidden">
            <button onClick={() => setDrillPath('mjst')}
                    className={`px-2 py-0.5 text-[10px] font-medium ${drillPath === 'mjst' ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              SEG → Store
            </button>
            <button onClick={() => setDrillPath('stmj')}
                    className={`px-2 py-0.5 text-[10px] font-medium ${drillPath === 'stmj' ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              Store → SEG
            </button>
          </div>
        </div>
      </div>

      {/* Inline "View by Grid" picker — shown at rollup/store levels in the
          standard drill. Click a chip → re-renders the current scope grouped
          by that grid dim. The crumb stays intact; user can clear and resume
          standard drill anytime. Hidden at deepest levels (OPT, Article) and
          when already in a grid branch. */}
      {!isGridBranch && (currentDim === 'SEG' || currentDim === 'DIV' ||
                          currentDim === 'SUB_DIV' || currentDim === 'MAJ_CAT' ||
                          currentDim === 'ST_CD') && (
        <div className="flex items-center gap-2 flex-wrap text-[10px] px-2 py-1 bg-purple-50/60 border border-purple-100 rounded-lg">
          <span className="font-bold uppercase tracking-wider text-purple-700">View fill-rate by grid</span>
          {GRID_DIMS.map(g => (
            <button key={g} onClick={() => setGridDim(g)}
                    title={`View ${GRID_LABELS[g]} (${g}) for current scope`}
                    className="px-2 py-0.5 rounded border border-purple-200 bg-white text-purple-700 hover:bg-purple-100 hover:border-purple-300">
              {GRID_LABELS[g]} <span className="text-purple-400">·</span> {g}
            </button>
          ))}
          <span className="text-gray-500 ml-auto">tip: click any chip to see fill-rate grouped by that grid</span>
        </div>
      )}

      {/* Body —
          Store level   (ST_CD)   → flat (Store × RDC) with ST_NM / HUB / RDC
          OPT   level   (GEN_ART) → flat (OPT × RDC) with OPT_TYPE/RANK/ALLOC_SEQ/STATUS/REMARKS
          Article level (ARTICLE) → flat (VAR_ART × SZ) — listing-page reference format
          Grid  branch  (gridDim active) → rich pivot grouped by selected grid dim
          everything else          → rich MAJ_CAT × RDC-style pivot                            */}
      {currentDim === 'ST_CD' ? (
        <FlatDrillTable
          rows={filter ? storeFlatRows.filter(r =>
                  String(r.st_cd ?? '').toLowerCase().includes(filter.toLowerCase()) ||
                  String(r.st_nm ?? '').toLowerCase().includes(filter.toLowerCase()) ||
                  String(r.hub   ?? '').toLowerCase().includes(filter.toLowerCase()))
                : storeFlatRows}
          columns={storeColumns}
          onRowClick={storeRowClick}
          emptyText="no store rows for this session" />
      ) : currentDim === 'GEN_ART' ? (
        <FlatDrillTable
          rows={filter ? optFlatRows.filter(r =>
                  String(r.label    ?? '').toLowerCase().includes(filter.toLowerCase()) ||
                  String(r.opt_type ?? '').toLowerCase().includes(filter.toLowerCase()) ||
                  String(r.status   ?? '').toLowerCase().includes(filter.toLowerCase()))
                : optFlatRows}
          columns={optColumns}
          onRowClick={optRowClick}
          emptyText="no OPTs for this scope" />
      ) : currentDim === 'ARTICLE' ? (
        <FlatDrillTable
          rows={filter ? articleRows.filter(r =>
                  String(r.var_art ?? '').toLowerCase().includes(filter.toLowerCase()) ||
                  String(r.sz      ?? '').toLowerCase().includes(filter.toLowerCase()) ||
                  String(r.status  ?? '').toLowerCase().includes(filter.toLowerCase()) ||
                  String(r.reason  ?? '').toLowerCase().includes(filter.toLowerCase()))
                : articleRows}
          columns={articleColumns}
          onRowClick={null}
          emptyText="no size-grain rows (alloc archive empty for this scope)" />
      ) : (
        <SessionReviewGrid data={pivot} filter={filter}
                           groupLabel={isGridBranch
                                       ? (GRID_LABELS[gridDim] || gridDim)
                                       : (DRILL_LABELS[currentDim] || currentDim)}
                           onRowClick={isGridBranch ? null : drillInto}
                           showRdc={isGridBranch ? false : effectiveShowRdc}
                           onToggleRdc={(canShowRdc && !isGridBranch)
                                        ? (() => setRdcOpen(o => !o)) : null} />
      )}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   Left rail: sessions grouped by day. Each day section can collapse.
───────────────────────────────────────────────────────────────────────── */
function SessionList({ sessions, activeSid, onPick, search }) {
  // Group by date-of-ts (newest day first)
  const groups = useMemo(() => {
    const filtered = (search || '').trim()
      ? sessions.filter(s => s.session_id.toLowerCase().includes(search.toLowerCase()))
      : sessions
    const m = new Map()
    filtered.forEach(s => {
      const d = dateOfTs(s.ts) || 'unknown'
      if (!m.has(d)) m.set(d, [])
      m.get(d).push(s)
    })
    return [...m.entries()].sort(([a], [b]) => b.localeCompare(a))
  }, [sessions, search])

  const [collapsed, setCollapsed] = useState(new Set())
  const toggle = (d) => setCollapsed(c => {
    const next = new Set(c)
    next.has(d) ? next.delete(d) : next.add(d)
    return next
  })

  if (!sessions.length) {
    return <div className="text-xs text-gray-400 px-3 py-6 text-center">No archived sessions in this range</div>
  }

  return (
    <div className="space-y-2">
      {groups.map(([date, items]) => {
        const isCollapsed = collapsed.has(date)
        return (
          <div key={date} className="bg-white border border-gray-200 rounded-md">
            <button onClick={() => toggle(date)}
                    className="w-full flex items-center justify-between px-2.5 py-1.5 text-xs font-semibold text-gray-700 hover:bg-gray-50 border-b border-gray-100">
              <span className="flex items-center gap-1.5">
                {isCollapsed ? <ChevronRight size={12} className="text-gray-400" />
                             : <ChevronDown  size={12} className="text-gray-400" />}
                {fmtDayLabel(date)}
              </span>
              <span className="text-[10px] text-gray-400">{items.length}</span>
            </button>
            {!isCollapsed && (
              <ul className="divide-y divide-gray-100">
                {items.map(s => {
                  const active = s.session_id === activeSid
                  const srcCls = s.src === 'history' ? 'bg-emerald-100 text-emerald-700'
                                                     : 'bg-amber-100 text-amber-700'
                  return (
                    <li key={s.session_id}>
                      <button onClick={() => onPick(s.session_id)}
                              className={`w-full text-left px-2.5 py-1.5 text-[11px] flex flex-col gap-0.5 transition-colors ${
                                active ? 'bg-indigo-50 border-l-4 border-indigo-600 -ml-px'
                                       : 'hover:bg-gray-50 border-l-4 border-transparent -ml-px'
                              }`}>
                        <div className="flex items-center justify-between gap-1">
                          <span className={`font-mono ${active ? 'text-indigo-700 font-semibold' : 'text-gray-700'}`}>
                            {s.session_id}
                          </span>
                          <span className={`px-1.5 py-0.5 text-[9px] uppercase tracking-wider rounded ${srcCls}`}>
                            {s.src}
                          </span>
                        </div>
                        <div className="flex items-center gap-1 text-[10px] text-gray-500">
                          <Clock size={10} /> {fmtTimeShort(s.ts)}
                        </div>
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
        )
      })}
    </div>
  )
}

/* ─────────────────────────────────────────────────────────────────────────
   Main page
───────────────────────────────────────────────────────────────────────── */
export default function AlcReviewPage() {
  const [searchParams, setSearchParams] = useSearchParams()

  // Default window: last 7 days
  const [from, setFrom]     = useState(searchParams.get('from') || minusDays(6))
  const [to,   setTo]       = useState(searchParams.get('to')   || today())
  const [src,  setSrc]      = useState(searchParams.get('src')  || 'all')   // all|parked|history
  const [activeSid, setActiveSid] = useState(searchParams.get('sid') || '')
  const [search, setSearch] = useState('')
  // Left rail can collapse so the right pane gets full width
  const [railOpen, setRailOpen] = useState(searchParams.get('rail') !== '0')

  const [sessions, setSessions] = useState([])
  const [loading,  setLoading]  = useState(false)

  const reload = useCallback(() => {
    setLoading(true)
    const params = { from, to, src }
    arsDashboardAPI.sessionsReviewList(params)
      .then(r => setSessions(Array.isArray(r?.data?.data?.items) ? r.data.data.items : []))
      .catch(() => setSessions([]))
      .finally(() => setLoading(false))
  }, [from, to, src])
  useEffect(() => { reload() }, [reload])

  // URL sync
  useEffect(() => {
    const p = new URLSearchParams()
    if (from) p.set('from', from)
    if (to)   p.set('to',   to)
    if (src && src !== 'all') p.set('src', src)
    if (activeSid) p.set('sid', activeSid)
    if (!railOpen) p.set('rail', '0')
    setSearchParams(p, { replace: true })
  }, [from, to, src, activeSid, railOpen])

  // Auto-pick the newest session in the list when none chosen / current one
  // dropped out of the filtered set.
  useEffect(() => {
    if (!sessions.length) return
    if (activeSid && sessions.some(s => s.session_id === activeSid)) return
    setActiveSid(sessions[0].session_id)
  }, [sessions, activeSid])

  // Quick-window presets
  const setWindow = (days) => {
    setFrom(minusDays(days - 1))
    setTo(today())
  }

  const counts = useMemo(() => {
    const c = { all: sessions.length, parked: 0, history: 0 }
    sessions.forEach(s => { c[s.src] = (c[s.src] || 0) + 1 })
    return c
  }, [sessions])

  const activeSession = useMemo(
    () => sessions.find(s => s.session_id === activeSid),
    [sessions, activeSid]
  )

  return (
    <div className="p-3 space-y-2 w-full">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <button onClick={() => setRailOpen(o => !o)}
                  title={railOpen ? 'Hide sessions rail' : 'Show sessions rail'}
                  className="p-1.5 text-gray-500 hover:text-gray-900 hover:bg-gray-100 rounded">
            {railOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
          </button>
          <div>
            <h1 className="text-base font-bold text-gray-900 flex items-center gap-2">
              <History size={16} className="text-indigo-600" /> Allocation Review
              {activeSession && (
                <span className="text-[11px] font-mono text-indigo-700 bg-indigo-50 px-2 py-0.5 rounded">
                  {activeSession.session_id} · {activeSession.src?.toUpperCase()}
                </span>
              )}
            </h1>
            <p className="text-[11px] text-gray-500 leading-tight">
              Session-wise review of listing + allocation from PARKED / HISTORY archives
            </p>
          </div>
        </div>
        <button onClick={reload} disabled={loading}
                className="flex items-center gap-2 px-3 py-1.5 text-xs bg-white border border-gray-200 rounded-lg hover:bg-gray-50 disabled:opacity-50">
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      {/* Filters */}
      <section className="bg-white border border-gray-200 rounded-xl px-3 py-1.5 flex items-center gap-2 flex-wrap shadow-sm">
        <span className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mr-1">Window</span>
        <div className="inline-flex border border-gray-200 rounded-md overflow-hidden">
          {[
            { d: 7,  label: '7d'  },
            { d: 14, label: '14d' },
            { d: 30, label: '30d' },
          ].map(p => {
            const active = from === minusDays(p.d - 1) && to === today()
            return (
              <button key={p.d} onClick={() => setWindow(p.d)}
                      className={`px-2.5 py-1 text-[11px] font-medium ${active ? 'bg-indigo-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
                Last {p.label}
              </button>
            )
          })}
        </div>
        <input type="date" value={from} onChange={e => setFrom(e.target.value)}
               className="text-xs border border-gray-200 rounded px-2 py-1 bg-white" />
        <span className="text-gray-400 text-xs">→</span>
        <input type="date" value={to} onChange={e => setTo(e.target.value)}
               className="text-xs border border-gray-200 rounded px-2 py-1 bg-white" />

        <span className="text-[10px] font-bold text-gray-500 uppercase tracking-wider ml-3 mr-1">Source</span>
        <div className="inline-flex border border-gray-200 rounded-md overflow-hidden">
          {[
            { v: 'all',     l: `All (${counts.all})`,           cls: 'bg-indigo-600' },
            { v: 'parked',  l: `Parked (${counts.parked})`,     cls: 'bg-amber-600'  },
            { v: 'history', l: `History (${counts.history})`,   cls: 'bg-emerald-600' },
          ].map(o => (
            <button key={o.v} onClick={() => setSrc(o.v)}
                    className={`px-2.5 py-1 text-[11px] font-medium ${src === o.v ? `${o.cls} text-white` : 'bg-white text-gray-600 hover:bg-gray-50'}`}>
              {o.l}
            </button>
          ))}
        </div>

        <div className="relative ml-auto">
          <Search size={12} className="absolute left-2 top-2 text-gray-400" />
          <input value={search} onChange={e => setSearch(e.target.value)}
                 placeholder="Search session…"
                 className="text-xs border border-gray-200 rounded pl-7 pr-2 py-1.5 bg-white w-56" />
        </div>
      </section>

      {/* Two-pane layout — rail can collapse so the detail pane fills the screen */}
      <div className="flex gap-2" style={{ minHeight: 'calc(100vh - 180px)' }}>
        {/* Left rail (collapsible) */}
        {railOpen && (
          <aside className="shrink-0 w-[230px] xl:w-[260px]">
            <div className="bg-gray-50 rounded-xl p-2 sticky top-2"
                 style={{ maxHeight: 'calc(100vh - 200px)', overflowY: 'auto' }}>
              <div className="flex items-center justify-between gap-1.5 text-[10px] font-bold text-gray-500 uppercase tracking-wider px-1 py-1">
                <span className="flex items-center gap-1.5">
                  <ClipboardList size={11} /> Sessions ({sessions.length})
                  {loading && <Loader2 size={11} className="animate-spin text-indigo-500" />}
                </span>
                <button onClick={() => setRailOpen(false)}
                        title="Hide rail"
                        className="p-0.5 text-gray-400 hover:text-gray-700 hover:bg-gray-200 rounded">
                  <ChevronLeft size={12} />
                </button>
              </div>
              <SessionList sessions={sessions} activeSid={activeSid}
                           onPick={setActiveSid} search={search} />
            </div>
          </aside>
        )}

        {/* Right pane — fills remaining width */}
        <div className="flex-1 min-w-0">
          <div className="bg-white border border-gray-200 rounded-xl shadow-sm p-3">
            {!activeSid ? (
              <div className="h-72 flex flex-col items-center justify-center gap-2 text-sm text-gray-400">
                <Archive size={32} className="text-gray-300" />
                Select a session from the {railOpen ? 'left' : 'sessions rail'} to begin reviewing
                {!railOpen && (
                  <button onClick={() => setRailOpen(true)}
                          className="mt-1 text-[11px] text-indigo-600 hover:underline">
                    Show sessions rail
                  </button>
                )}
              </div>
            ) : (
              <SessionDrillView sid={activeSid} />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
