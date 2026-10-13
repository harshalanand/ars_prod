import { Building2, Network, ShieldCheck, ShieldOff, GitMerge, GitBranch, History } from 'lucide-react'
import { C } from '@/theme/colors'
import { Tip, CROSS_WORDS } from './Sz1Ui'

// 1SZ ALC — small pieces shared by the Run wizard and the Sessions page:
// the warehouse option words, chips and the simulate variant keys.
// Spec: public/docs/manual/sz1_alloc.md §D (Warehouse) and §F (Sessions).

// Own RDC = teal, All RDC = violet. The theme has no teal / violet, so they live here.
export const WH = {
  same: { fg: '#0f766e', bg: '#f0fdfa', bd: '#99f6e4', icon: Building2 },
  all: { fg: '#6d28d9', bg: '#f5f3ff', bd: '#ddd6fe', icon: Network },
}

export const PRIORITY_WORDS = { weighted: 'Balanced', mj_srt: 'Biggest total shortage', sequence: 'My order' }

export const WH_HELP = {
  same: 'Each store from its home warehouse. No splits. A store short of a size only the other warehouse holds stays short.',
  all: 'Pool DH24 + DW01, allocate in priority order, split by source.',
  protect: 'Stock the other warehouse’s own listed stores need is held back; only its surplus moves.',
  one: 'Fill a store’s line from one warehouse; split only when one can’t cover it.',
}

/** The warehouse options of a session: summary.options, else the CROSS_RDC column (older sessions). */
export function whOptions(s) {
  const o = s?.summary?.options || {}
  const cross = o.cross_rdc || s?.CROSS_RDC
  const known = o.protect_other != null || o.one_source != null
  return { cross, protect: o.protect_other, one: o.one_source, known }
}

/** A stable key per warehouse option set: own · all · all_unprot · all_split · all_unprot_split. */
export function variantKey(v) {
  if (v.cross_rdc !== 'all') return 'own'
  return `all${v.protect_other === false ? '_unprot' : ''}${v.one_source === false ? '_split' : ''}`
}

export function variantLabel(v) {
  if (v.cross_rdc !== 'all') return 'Own RDC'
  return `All RDC · ${v.protect_other === false ? 'unprotected' : 'protected'} · ${v.one_source === false ? 'split freely' : 'one source'}`
}

/** Short column label for tables. */
export function shortLabel(v) {
  if (v.cross_rdc !== 'all') return 'Own RDC'
  const bits = [v.protect_other === false && 'unprotected', v.one_source === false && 'split freely'].filter(Boolean)
  return bits.length ? `All RDC (${bits.join(', ')})` : 'All RDC'
}

function WChip({ tone, icon: Icon, children, tip }) {
  const t = tone || { fg: C.textSub, bg: '#f1f5f9', bd: C.cardBorder }
  return (
    <Tip tip={tip}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11.5, fontWeight: 700, color: t.fg, background: t.bg,
                     border: `1px solid ${t.bd}`, borderRadius: 999, padding: '2px 9px', whiteSpace: 'nowrap', cursor: tip ? 'help' : 'default' }}>
        {Icon && <Icon size={12} />}{children}
      </span>
    </Tip>
  )
}

const AMBER = { fg: C.amber, bg: C.amberBg, bd: C.amberBd }

/**
 * The warehouse mode as chips: Own / All, then Protected and One source (All RDC only).
 * cross = same | all | legacy; protect / one = booleans or null (not recorded).
 */
export function WarehouseChips({ cross, protect, one, known = true }) {
  if (cross === 'same') return <WChip tone={WH.same} icon={Building2} tip={{ title: 'Own RDC', body: WH_HELP.same }}>Own RDC</WChip>
  if (cross !== 'all') {
    return <WChip icon={History} tip="A legacy warehouse mode, kept for parity with the tool.">{CROSS_WORDS[cross] || cross || '—'}</WChip>
  }
  return (
    <span style={{ display: 'inline-flex', gap: 5, flexWrap: 'wrap' }}>
      <WChip tone={WH.all} icon={Network} tip={{ title: 'All RDC', body: WH_HELP.all }}>All RDC</WChip>
      {!known ? (
        <WChip icon={History} tip="This session ran before the Protect and One-source switches existed: the shared pool, no reserve for the other warehouse’s stores.">
          Before the switches
        </WChip>
      ) : (
        <>
          {protect === false
            ? <WChip tone={AMBER} icon={ShieldOff} tip={{ title: 'Unprotected', body: 'The other warehouse’s stock was open to every store in priority order, with nothing held back for its own stores.' }}>Unprotected</WChip>
            : <WChip tone={WH.all} icon={ShieldCheck} tip={{ title: 'Protected', body: WH_HELP.protect }}>Protected</WChip>}
          {one === false
            ? <WChip tone={AMBER} icon={GitBranch} tip={{ title: 'Split freely', body: 'A line could take packs from both warehouses.' }}>Split freely</WChip>
            : <WChip tone={WH.all} icon={GitMerge} tip={{ title: 'One source', body: WH_HELP.one }}>One source</WChip>}
        </>
      )}
    </span>
  )
}

/** Just the warehouse mode (Own / All) — for lists whose rows carry no summary. */
export function WarehouseMode({ cross }) {
  if (cross === 'same') return <WChip tone={WH.same} icon={Building2} tip={{ title: 'Own RDC', body: WH_HELP.same }}>Own RDC</WChip>
  if (cross === 'all') return <WChip tone={WH.all} icon={Network} tip={{ title: 'All RDC', body: WH_HELP.all }}>All RDC</WChip>
  return <WChip icon={History} tip="A legacy warehouse mode, kept for parity with the tool.">{CROSS_WORDS[cross] || cross || '—'}</WChip>
}

// ── session lifecycle (P1–P9): RUNNING → PARKED → APPROVED (Pend ALC) | DISCARDED ──
// A legacy 'DONE' session is a parked one.
export const STALE_DAYS = 2
export const statusOf = s => {
  const v = String((s && typeof s === 'object' ? s.STATUS : s) || '').toUpperCase()
  return v === 'DONE' ? 'PARKED' : v
}
export const isParked = s => statusOf(s) === 'PARKED'
export const isApproved = s => statusOf(s) === 'APPROVED'
export const isClosed = s => ['DISCARDED', 'FAILED', 'CANCELLED'].includes(statusOf(s))

export const STATUS_META = {
  RUNNING: { label: 'Running', fg: C.indigo, bg: C.indigoBg, tip: 'The run is working. It parks when it commits.' },
  PARKED: { label: 'Parked', fg: C.amber, bg: C.amberBg, tip: 'Holds its stock and size budget inside 1SZ. Approve it into Pend ALC, or discard it.' },
  APPROVED: { label: 'Approved', fg: C.green, bg: C.greenBg, tip: 'Written to ARS_PEND_ALC. Pend ALC now manages its BDC, DO and closing.' },
  DISCARDED: { label: 'Discarded', fg: C.gray, bg: C.grayBg, tip: 'Dropped. Its stock and size budget went back to the pool.' },
  FAILED: { label: 'Failed', fg: C.red, bg: C.redBg, tip: 'The run stopped with an error and wrote no lines.' },
  CANCELLED: { label: 'Cancelled', fg: C.gray, bg: C.grayBg, tip: 'Stopped by a user. It wrote no lines.' },
}

/** The lifecycle status as a coloured pill with a hover tip. */
export function StatusBadge({ s }) {
  const k = statusOf(s)
  const m = STATUS_META[k] || { label: k || '—', fg: C.gray, bg: C.grayBg }
  return (
    <Tip tip={m.tip && { title: m.label, body: m.tip }}>
      <span style={{ fontSize: 11, fontWeight: 700, color: m.fg, background: m.bg, borderRadius: 999, padding: '3px 10px', whiteSpace: 'nowrap',
                     cursor: m.tip ? 'help' : 'default' }}>{m.label}</span>
    </Tip>
  )
}

/** Whole days since a timestamp (null when unknown). */
export function ageDays(ts) {
  if (!ts) return null
  const t = new Date(String(ts).replace(' ', 'T')).getTime()
  return isNaN(t) ? null : (Date.now() - t) / 86400000
}
export const isStale = s => isParked(s) && (ageDays(s.SESSION_AT) ?? 0) > STALE_DAYS

/** "parked 3 d" / "parked 5 h" — amber when older than STALE_DAYS. */
export function AgeChip({ s, verb = 'parked' }) {
  const d = ageDays(s?.SESSION_AT)
  if (d == null) return null
  const txt = d >= 1 ? `${Math.floor(d)} d` : d * 24 >= 1 ? `${Math.floor(d * 24)} h` : `${Math.max(1, Math.round(d * 1440))} min`
  const old = d > STALE_DAYS
  return (
    <Tip tip={old ? `Parked for more than ${STALE_DAYS} days: its stock is held back from every other run. Approve it or discard it.`
      : 'How long this session has held its stock.'}>
      <span style={{ fontSize: 11, fontWeight: 700, whiteSpace: 'nowrap', borderRadius: 999, padding: '2px 8px', cursor: 'help',
                     color: old ? C.amber : C.textSub, background: old ? C.amberBg : '#f1f5f9', border: `1px solid ${old ? C.amberBd : 'transparent'}` }}>
        {verb} {txt}
      </span>
    </Tip>
  )
}

/** Parked count + units held, from a session list. */
export function parkedSummary(list) {
  const parked = (list || []).filter(isParked)
  return { count: parked.length, units: parked.reduce((a, s) => a + (+s.ALLOC_QTY || 0), 0),
           stale: parked.filter(isStale).length, ids: parked.map(s => s.SESSION_ID) }
}

const today = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
export const approvedToday = list => (list || []).filter(s => isApproved(s) && String(s.APPROVED_AT || '').slice(0, 10) === today()).length

/** A 403 on approve / revert reads as a permission message. */
export function actErr(e, fb) {
  if (e?.response?.status === 403) {
    return 'You need the ACT_1SZ_APPROVE permission to approve or revert 1SZ sessions. Ask an administrator.'
  }
  const d = e?.response?.data?.detail
  return (typeof d === 'string' ? d : d?.message) || e?.message || fb
}

/** A small section heading. */
export function SubHead({ children, right }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11, fontWeight: 800, letterSpacing: '.06em', textTransform: 'uppercase',
                  color: C.textSub, margin: '2px 0 8px' }}>
      {children}{right && <span style={{ marginLeft: 'auto', textTransform: 'none', letterSpacing: 0, fontWeight: 600 }}>{right}</span>}
    </div>
  )
}
