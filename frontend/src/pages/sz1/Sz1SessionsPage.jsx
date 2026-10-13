import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Download, Trash2, Search, ClipboardCheck, Truck, Boxes, Store, Network, GitBranch, Target,
  ChevronLeft, ChevronRight, ChevronDown, Clock, User, Send, Scissors, RefreshCw, XCircle, Undo2, ExternalLink,
  ParkingSquare, CheckCircle2, Archive, Cpu,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API, saveBlob } from '@/services/api'
import { confirmDialog } from '@/components/ui/ConfirmDialog'
import { C } from '@/theme/colors'
import {
  Sz1Header, Panel, Banner, Kpi, DataTable, Checks, Btn, Chip, Tip,
  n, when, pct, errText, BASIS_WORDS,
} from '@/components/sz1/Sz1Ui'
import { ACTION_HELP } from '@/components/sz1/sz1Help'
import {
  WarehouseChips, WarehouseMode, whOptions, SubHead, PRIORITY_WORDS, StatusBadge, AgeChip,
  statusOf, isParked, isApproved, isClosed, isStale, parkedSummary, approvedToday, actErr, STALE_DAYS,
} from '@/components/sz1/Sz1SessionBits'
import { PendProgress, pendState, SessionTimeline, ApproveModal, RemoveStoresModal } from '@/components/sz1/Sz1SessionActions'

// 1SZ ALC — Sessions: every Step 3 run parks and holds its stock inside 1SZ;
// a parked session is approved into ARS_PEND_ALC (Pend ALC then runs BDC → DO
// → closed) or discarded. Spec: public/docs/manual/sz1_alloc.md §E–F, §J (P3).
const STORE_COLS = [['ST_CD', 'Store'], ['ST_NM', 'Name'], ['RDC', 'RDC'], ['AVG_SEQ', 'Avg seq'],
  ['LINES', 'Lines'], ['ARTICLES', 'Articles'], ['MAJ_CATS', 'MAJ_CATs'], ['MAX_I_ROD', 'Max I_ROD'], ['ALC_I_ROD', 'Last round'],
  ['SZ_SRT', 'Size shortage'], ['SHORT_QTY', 'Asked 1 rnd'], ['SHORT_QTY_ROD', 'Asked all'], ['ALLOC_QTY', 'Allocated'],
  ['HOME_QTY', 'Home'], ['CROSS_QTY', 'Other RDC'], ['PACKS', 'Packs'], ['SERVED_LINES', 'Lines served'], ['FILL_PCT', 'Fill %']]

const TABS = [
  ['stores', 'Per store', 'What each store asked for and got.', sm => sm.per_store?.length],
  ['warehouse', 'Warehouses', 'What each RDC sent to its own stores and to the other RDC’s.', sm => sm.by_src?.length],
  ['cats', 'Categories', 'The 40 categories that received the most.', sm => sm.by_cat?.length],
  ['rounds', 'Rounds', 'Which refill round shipped, what extra rounds let in, and real sizes vs full options.'],
  ['lines', 'Lines', 'Every line, filtered and paged.'],
  ['settings', 'Settings used', 'Every choice the run was made with, the builds it read, its stores and skipped codes.'],
]

const PEND_RECO = '/pend-alc/reco'
const stockWords = (s, o = s?.summary?.options || {}) => (o.stock_basis === 'live' || isParked(s) || isApproved(s)
  ? 'Live free stock' : BASIS_WORDS[s?.STOCK_BASIS] || s?.STOCK_BASIS || '—')

export default function Sz1SessionsPage() {
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const [list, setList] = useState([])
  const [pend, setPend] = useState({})
  const [running, setRunning] = useState(null)
  const [status, setStatus] = useState(null)
  const [s, setS] = useState(null)
  const [tab, setTab] = useState('stores')
  const [showClosed, setShowClosed] = useState(false)
  const [modal, setModal] = useState('')              // approve | remove
  const [busy, setBusy] = useState('')
  const [detailKey, setDetailKey] = useState(0)        // bump to read the open session again
  const sid = params.get('id')

  const load = useCallback(async () => {
    try {
      const [{ data: r }, { data: st }] = await Promise.all([sz1API.sessions(200), sz1API.status()])
      const rows = r.data.sessions || []
      setList(rows); setPend(r.data.pend || {}); setRunning(r.data.running); setStatus(st.data)
      if (!sid && rows.length) setParams({ id: (rows.find(isParked) || rows[0]).SESSION_ID }, { replace: true })
    } catch (e) { toast.error(errText(e, 'Could not load sessions')) }
  }, [sid, setParams])
  useEffect(() => { load() }, [load])

  // read the open session; while it runs (a new run or a re-fit), poll it until it parks
  useEffect(() => {
    if (!sid) { setS(null); return undefined }
    let alive = true
    let timer = null
    let wasRunning = false
    const tick = async () => {
      try {
        const { data: r } = await sz1API.runSession(sid)
        if (!alive) return
        setS(r.data)
        if (r.data.STATUS === 'RUNNING') { wasRunning = true; timer = setTimeout(tick, 2000); return }
        if (wasRunning) {
          const st = statusOf(r.data)
          st === 'PARKED' ? toast.success(`Session ${sid} parked — approve it into Pend ALC when ready`) : toast.error(`Session ${sid}: ${r.data.progress?.text || st}`)
          load()
        }
      } catch (e) { if (alive) { setS(null); toast.error(errText(e, 'Could not load the session')) } }
    }
    tick()
    return () => { alive = false; clearTimeout(timer) }
  }, [sid, detailKey, load])

  const refresh = () => { load(); setDetailKey(k => k + 1) }
  const open = id => setParams({ id })

  // ── actions ──
  const del = async (row) => {
    const ok = await confirmDialog({
      tone: 'danger', title: `Delete session ${row.SESSION_ID}?`, confirmLabel: 'Delete',
      body: `${n(row.ROWS_WRITTEN)} lines and the session header are removed from 1SZ.`,
      bullets: ['It holds no stock, so nothing else changes.', 'This cannot be undone.'],
    })
    if (!ok) return
    try {
      const { data: r } = await sz1API.deleteSession(row.SESSION_ID)
      toast.success(r.message || `Session ${row.SESSION_ID} deleted`)
      if (sid === row.SESSION_ID) setParams({}, { replace: true })
      load()
    } catch (e) { toast.error(errText(e, 'Could not delete')) }
  }

  const discard = async (row) => {
    const ok = await confirmDialog({
      tone: 'danger', title: `Discard session ${row.SESSION_ID}?`, confirmLabel: 'Discard',
      body: `Its ${n(row.ALLOC_QTY)} units and the size budget it holds go back to the pool for other runs.`,
      bullets: ['Nothing reaches Pend ALC.', 'The session stays on record as Discarded (you can delete it afterwards).', 'This cannot be undone — run the stores again to allocate them.'],
    })
    if (!ok) return
    setBusy('discard')
    try {
      const { data: r } = await sz1API.discard(row.SESSION_ID)
      toast.success(r.message || `Session ${row.SESSION_ID} discarded`)
      refresh()
    } catch (e) { toast.error(actErr(e, 'Could not discard')) }
    finally { setBusy('') }
  }

  const refit = async (row) => {
    if (busy) return                                  // the Approve popup stays open while a re-fit starts
    const ok = await confirmDialog({
      tone: 'warn', title: `Re-fit session ${row.SESSION_ID}?`, confirmLabel: 'Re-fit',
      body: 'Runs the same stores and options again on the stock that is free now.',
      bullets: [`Session ${row.SESSION_ID} is discarded first: its stock goes back to the pool.`,
                'The new session parks when it finishes; check it and approve it then.',
                'It may allocate less if other sessions or listing took the stock.'],
    })
    if (!ok) return
    setBusy('refit')
    try {
      const { data: r } = await sz1API.refit(row.SESSION_ID)
      const id = (r.data || r).session_id
      toast(r.message || `Re-fit started as session ${id}`)
      setModal('')
      if (id) setParams({ id })
      load()
    } catch (e) { toast.error(actErr(e, 'Could not start the re-fit')) }
    finally { setBusy('') }
  }

  const revert = async (row) => {
    const ok = await confirmDialog({
      tone: 'danger', title: `Revert session ${row.SESSION_ID} out of Pend ALC?`, confirmLabel: 'Revert',
      body: `Its rows leave ARS_PEND_ALC (session ${row.PEND_SESSION_ID || `1SZ-${row.SESSION_ID}`}) and the session goes back to Parked.`,
      bullets: ['MSA free stock and the grid are given back at once.', 'Only possible while no BDC is sent and no DO is received for it.',
                'The parked session still holds its stock in 1SZ: approve it again or discard it.'],
    })
    if (!ok) return
    setBusy('revert')
    try {
      const { data: r } = await sz1API.revert(row.SESSION_ID)
      const d = r.data || {}
      if (d.ok === false) toast.error(`Cannot revert: ${(d.reasons || []).join(' · ') || 'Pend ALC has moved on'}`, { duration: 7000 })
      else toast.success(r.message || `Session ${row.SESSION_ID} is parked again`)
      refresh()
    } catch (e) { toast.error(actErr(e, 'Could not revert')) }
    finally { setBusy('') }
  }

  // Pend ALC → Reconciliation reads ?session= and filters its rows (open and closed) to this session.
  const openPend = (row) => {
    const pid = row.PEND_SESSION_ID || `1SZ-${row.SESSION_ID}`
    navigator.clipboard?.writeText(pid).catch(() => {})       // bonus: the id is on the clipboard too
    nav(`${PEND_RECO}?session=${encodeURIComponent(pid)}`)
  }

  // ── groups ──
  const parked = list.filter(isParked)
  const approved = list.filter(isApproved)
  const closed = list.filter(isClosed)
  const runningRows = list.filter(x => statusOf(x) === 'RUNNING')
  const ps = parkedSummary(list)
  const shipped = approved.filter(x => pendState(pend[x.SESSION_ID])?.shipped).length
  const cur = s && list.find(x => x.SESSION_ID === s.SESSION_ID)

  return (
    <div className="space-y-3" style={{ paddingBottom: 12 }}>
      <Sz1Header current="sessions" title="Sessions" icon={ClipboardCheck} status={status}
        subtitle="Every run parks and holds its stock and size budget inside 1SZ. Approve a parked session into Pend ALC (it then manages BDC, DO and closing), or discard it. Approve re-checks live stock first."
        onRefresh={refresh} refreshTip="Reload the sessions and the open one"
        stats={[
          { k: 'Parked', v: `${n(ps.count)} · ${n(ps.units)} u`, x: ps.stale ? `${ps.stale} older than ${STALE_DAYS} d` : 'holding stock in 1SZ',
            tip: 'Parked sessions and the units they hold back from every other run.', tone: ps.stale ? 'amber' : 'indigo' },
          { k: 'Approved', v: n(approved.length), x: `${approvedToday(list)} today · ${shipped} shipped`, tone: 'green',
            tip: 'Sessions written to ARS_PEND_ALC. Shipped = every Pend ALC row closed.' },
          { k: 'Closed', v: n(closed.length), x: 'discarded, failed, cancelled', tip: 'Sessions that hold no stock and never reached Pend ALC.' },
        ]} />
      {running?.kind === 'run' && <Banner tone="blue" label="Running" text={`Session ${running.id} is running. It parks here when it commits.`}
        action={sid !== String(running.id) && <Btn icon={Cpu} onClick={() => open(String(running.id))} tip="Watch the run">Open</Btn>} />}
      {ps.stale > 0 && <Banner tone="amber" label="Waiting" text={`${ps.stale} parked session${ps.stale > 1 ? 's are' : ' is'} older than ${STALE_DAYS} days and still hold${ps.stale > 1 ? '' : 's'} stock back from every other run. Approve or discard ${ps.stale > 1 ? 'them' : 'it'}.`} />}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(460px, 100%), 1fr))', gap: 12, alignItems: 'start' }}>
        <Panel title={`Parked (${parked.length + runningRows.length})`} icon={ParkingSquare} pad={0} collapseKey="sz1-parked"
          tip="Holding stock and size budget inside 1SZ. Approve into Pend ALC, or discard."
          right={<Tip tip="Units the parked sessions hold back from every other run"><Chip tone="amber">{n(ps.units)} units held</Chip></Tip>}>
          <SessionRows rows={[...runningRows, ...parked]} sid={sid} onOpen={open} empty="No parked session. Plan a run to make one." />
        </Panel>
        <Panel title={`Approved (${approved.length})`} icon={Send} pad={0} collapseKey="sz1-approved"
          tip="In ARS_PEND_ALC. Pend ALC manages BDC, DO and closing; revert is possible until a BDC or DO."
          right={<Tip tip="Approved sessions whose Pend ALC rows are all closed"><Chip tone="green">{n(shipped)} shipped</Chip></Tip>}>
          <SessionRows rows={approved} pend={pend} sid={sid} onOpen={open} empty="Nothing approved yet." />
        </Panel>
      </div>

      <section style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 14, boxShadow: '0 1px 3px rgba(15,23,42,.05)' }}>
        <Tip tip={showClosed ? 'Hide the closed sessions' : 'Show the discarded, failed and cancelled sessions'} block>
          <button type="button" onClick={() => setShowClosed(v => !v)} aria-expanded={showClosed}
            style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 9, padding: '10px 16px', background: 'none', border: 'none', cursor: 'pointer', textAlign: 'left' }}>
            {showClosed ? <ChevronDown size={14} color={C.textSub} /> : <ChevronRight size={14} color={C.textSub} />}
            <Archive size={15} color={C.textSub} />
            <span style={{ fontSize: 13.5, fontWeight: 700, color: C.text }}>Closed ({closed.length})</span>
            <span style={{ fontSize: 11.5, color: C.textMuted }}>discarded · failed · cancelled — hold no stock</span>
          </button>
        </Tip>
        {showClosed && <div style={{ borderTop: `1px solid ${C.cardBorder}` }}><SessionRows rows={closed} sid={sid} onOpen={open} empty="No closed session." /></div>}
      </section>

      {s && (
        <Detail s={s} cur={cur} pendRow={pend[s.SESSION_ID]} tab={tab} setTab={setTab} busy={busy}
          onOpen={open} onApprove={() => setModal('approve')} onRemove={() => setModal('remove')}
          onRefit={() => refit(s)} onDiscard={() => discard(cur || s)} onRevert={() => revert(s)} onPend={() => openPend(s)}
          onDelete={() => del(cur || s)} />
      )}

      {s && (
        <>
          <ApproveModal s={s} open={modal === 'approve'} onClose={() => setModal('')} onRefit={() => refit(s)}
            onApproved={() => { setModal(''); refresh() }} />
          <RemoveStoresModal s={s} open={modal === 'remove'} onClose={() => setModal('')} onDone={() => { setModal(''); refresh() }} />
        </>
      )}
    </div>
  )
}

/** A compact session list: id + age, status, warehouse, stores, units; Pend ALC progress for approved ones. */
function SessionRows({ rows, pend, sid, onOpen, empty }) {
  if (!rows.length) return <div style={{ padding: 14, fontSize: 12.5, color: C.textMuted }}>{empty}</div>
  return (
    <div style={{ maxHeight: 300, overflow: 'auto' }}>
      {rows.map((r, i) => {
        const on = r.SESSION_ID === sid
        const st = statusOf(r)
        return (
          <div key={r.SESSION_ID} role="button" tabIndex={0} onClick={() => onOpen(r.SESSION_ID)} onKeyDown={e => { if (e.key === 'Enter') onOpen(r.SESSION_ID) }}
            className="sz1-sess-row"
            style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '9px 14px', cursor: 'pointer',
                     borderTop: i ? '1px solid #f1f5f9' : 'none', background: on ? C.primaryLt : undefined,
                     borderLeft: `3px solid ${on ? C.primary : isStale(r) ? C.amber : 'transparent'}` }}>
            <div style={{ minWidth: 0, flex: '1 1 130px' }}>
              <div style={{ fontSize: 12.5, fontWeight: 700, color: C.text, fontVariantNumeric: 'tabular-nums' }}>
                {r.SESSION_ID}{r.REFIT_OF && <span style={{ fontSize: 10.5, fontWeight: 600, color: C.textMuted }}> · re-fit of {r.REFIT_OF}</span>}
              </div>
              <div style={{ fontSize: 11, color: C.textMuted, whiteSpace: 'nowrap' }}>{when(r.SESSION_AT)} · {r.CREATED_BY || '—'}</div>
            </div>
            <span style={{ display: 'inline-flex', gap: 5, alignItems: 'center', flexWrap: 'wrap' }}>
              <StatusBadge s={r} />
              {st === 'PARKED' && <AgeChip s={r} />}
              <WarehouseMode cross={r.CROSS_RDC} />
            </span>
            {st === 'APPROVED' && <span style={{ flex: '1 1 100%', order: 3 }}><PendProgress p={pend?.[r.SESSION_ID]} width={84} /></span>}
            <Tip tip={`${n(r.STORES_SERVED)} stores served of ${n(r.STORES_OK)} in the run · ${n(r.PACKS)} packs`}>
              <span style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums', minWidth: 78, marginLeft: 'auto', cursor: 'help' }}>
                <span style={{ display: 'block', fontSize: 13, fontWeight: 800, color: C.text }}>{n(r.ALLOC_QTY)}</span>
                <span style={{ display: 'block', fontSize: 11, color: C.textMuted }}>{n(r.STORES_SERVED)} stores</span>
              </span>
            </Tip>
          </div>
        )
      })}
      <style>{`.sz1-sess-row:hover { background: ${C.primaryLt} }`}</style>
    </div>
  )
}

function Detail({ s, cur, pendRow, tab, setTab, busy, onOpen, onApprove, onRemove, onRefit, onDiscard, onRevert, onPend, onDelete }) {
  const sm = s.summary || {}
  const t = sm.totals || {}
  const wh = whOptions(s)
  const st = statusOf(s)
  const [exporting, setExporting] = useState('')
  const [pend, setPend] = useState(pendRow || null)
  useEffect(() => {
    setPend(pendRow || null)
    if (st !== 'APPROVED') return undefined
    let alive = true
    sz1API.sessionPend(s.SESSION_ID).then(({ data: r }) => { if (alive) { const d = r.data || r; setPend(d.pend || d) } }).catch(() => {})
    return () => { alive = false }
  }, [s.SESSION_ID, st, pendRow])
  const exp = async (only) => {
    setExporting(only)
    try { const res = await sz1API.exportSession(s.SESSION_ID, only); toast.success(`Saved ${saveBlob(res, `1SZ_ALC_${s.SESSION_ID}.csv`)}`) }
    catch (e) { toast.error(errText(e, 'Export failed')) }
    finally { setExporting('') }
  }
  const hasLines = ['PARKED', 'APPROVED', 'DISCARDED'].includes(st)
  const tabs = hasLines ? TABS : TABS.filter(([k]) => k === 'settings')
  const curTab = tabs.some(([k]) => k === tab) ? tab : tabs[0][0]
  const accent = { PARKED: C.amber, APPROVED: C.green, RUNNING: C.primary, DISCARDED: C.gray }[st] || C.red
  const pendId = s.PEND_SESSION_ID || `1SZ-${s.SESSION_ID}`
  const p = pendState(pend)
  const danger = { color: C.red, borderColor: C.redBd }
  const exports = hasLines && (
    <>
      <Btn icon={Download} onClick={() => exp('alloc')} busy={exporting === 'alloc'} disabled={!!exporting} tip={ACTION_HELP.exportAlloc}>Allocated CSV</Btn>
      <Btn icon={Download} onClick={() => exp('all')} busy={exporting === 'all'} disabled={!!exporting} tip={ACTION_HELP.exportAll}>All lines CSV</Btn>
    </>
  )
  return (
    <div className="space-y-3">
      <Panel title={`Session ${s.SESSION_ID}`} icon={ClipboardCheck} accent={accent} collapseKey="session-detail"
        right={<StatusBadge s={s} />}>
        {/* action bar by status */}
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 12 }}>
          {st === 'PARKED' && (
            <>
              <Btn kind="primary" icon={Send} onClick={onApprove} disabled={!!busy}
                tip={{ title: 'Approve → Pend ALC', body: 'Re-checks live stock and size budgets first, then writes the lines to ARS_PEND_ALC. Needs ACT_1SZ_APPROVE.' }}>
                Approve → Pend ALC</Btn>
              <Btn icon={Scissors} onClick={onRemove} disabled={!!busy} tip="Drop some stores from this parked session; their units go back to the pool.">Remove stores</Btn>
              <Btn icon={RefreshCw} onClick={onRefit} busy={busy === 'refit'} disabled={!!busy}
                tip="Run the same stores and options again on the stock that is free now. This session is discarded; the new one parks.">Re-fit</Btn>
              <Btn icon={XCircle} onClick={onDiscard} busy={busy === 'discard'} disabled={!!busy} style={danger}
                tip="Drop this session. Its stock and size budget go back to the pool; nothing reaches Pend ALC.">Discard</Btn>
            </>
          )}
          {st === 'APPROVED' && (
            <>
              <Btn kind="primary" icon={ExternalLink} onClick={onPend}
                tip={`Open Pend ALC → Reconciliation filtered to session ${pendId} (open and closed rows). The id is copied too.`}>Open in Pend ALC</Btn>
              <Btn icon={Undo2} onClick={onRevert} busy={busy === 'revert'} disabled={!!busy || (p && (p.bdc > 0 || p.dlv > 0))} style={danger}
                tip={p && (p.bdc > 0 || p.dlv > 0) ? 'A BDC is sent or a DO is back for this session: it can no longer be reverted.'
                  : 'Take the rows back out of ARS_PEND_ALC and park the session again. Only before any BDC or DO. Needs ACT_1SZ_APPROVE.'}>Revert</Btn>
            </>
          )}
          {s.REFIT_OF && (
            <Tip tip="This session re-ran an older one on the stock that was left. Open the old one.">
              <button type="button" onClick={() => onOpen(s.REFIT_OF)} style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer' }}>
                <Chip tone="indigo">re-fit of {s.REFIT_OF}</Chip>
              </button>
            </Tip>
          )}
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {exports}
            {['DISCARDED', 'FAILED', 'CANCELLED'].includes(st) && <Btn icon={Trash2} onClick={onDelete} tip={ACTION_HELP.del}>Delete</Btn>}
          </span>
        </div>

        <div style={{ marginBottom: 12 }}><SessionTimeline s={s} pend={pend} /></div>

        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', marginBottom: 14 }}>
          <WarehouseChips {...wh} />
          <Tip tip="Which store was served first inside each category + size"><Chip>{PRIORITY_WORDS[s.PRIORITY_MODE] || s.PRIORITY_MODE}{s.WEIGHT_MJ != null ? ` ${s.WEIGHT_MJ}` : ''}</Chip></Tip>
          <Tip tip="Every run uses the live free stock left after the parked sessions"><Chip>{stockWords(s)}</Chip></Tip>
          {st === 'APPROVED' && <Tip tip="The session id of these rows in ARS_PEND_ALC (SOURCE 1SZ)"><Chip tone="green">Pend ALC {pendId}</Chip></Tip>}
          {s.NOTE && <Tip tip="Note given at run time"><Chip tone="blue">“{s.NOTE}”</Chip></Tip>}
          <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 12, fontSize: 11.5, color: C.textMuted, flexWrap: 'wrap' }}>
            <Tip tip="When the run started"><span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><Clock size={12} />{when(s.SESSION_AT)} · {Math.round(s.DURATION_SEC || 0)} s</span></Tip>
            <Tip tip="Who ran it"><span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}><User size={12} />{s.CREATED_BY || '—'}</span></Tip>
          </span>
        </div>

        {st === 'APPROVED' && (
          <div style={{ marginBottom: 14, padding: '10px 12px', borderRadius: 10, border: `1px solid ${C.greenBd}`, background: '#f6fef9',
                        display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <CheckCircle2 size={16} color={C.green} />
            <span style={{ fontSize: 12.5, fontWeight: 700, color: C.text }}>In Pend ALC</span>
            <PendProgress p={pend} width={110} />
            {pend && <span style={{ marginLeft: 'auto', fontSize: 11.5, color: C.textSub }}>{n(pend.pend_qty)} units still pending · {n(pend.open_rows)} open rows</span>}
          </div>
        )}

        {st === 'RUNNING' ? (
          <div style={{ display: 'grid', gap: 8 }}>
            <Banner tone="blue" label="Running" text={`${s.progress?.text || 'Working'} · ${s.PROGRESS_PCT || 0}%. It parks here when it commits.`} />
            <div style={{ height: 8, borderRadius: 999, background: '#eef2f7', overflow: 'hidden' }}>
              <div style={{ width: `${s.PROGRESS_PCT || 0}%`, height: '100%', background: 'linear-gradient(90deg, #4f46e5, #7c3aed)', transition: 'width .4s' }} />
            </div>
          </div>
        ) : !hasLines ? (
          <div style={{ display: 'grid', gap: 12 }}>
            <Banner tone={st === 'CANCELLED' ? 'amber' : 'red'} label={st} text={`${s.progress?.text || ''} ${String(s.ERROR || '').split('\n')[0]}`} />
            {(s.checks || []).length > 0 && <Checks items={s.checks} />}
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 16 }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 10 }}>
              <Kpi k="Stores" v={`${n(s.STORES_OK)} / ${n(s.STORES_GIVEN)}`} x={`${n(s.STORES_SERVED)} served`} icon={Store} tone="blue" tip="Ready / supplied; served = got at least one pack." />
              <Kpi k="Size shortage" v={n(t.SZ_SRT)} x={`asked ${n(s.ASK_QTY_ROD)} (all rounds)`} icon={Target} tip="Σ MJ_SZ_SRT of the run’s lines: the budget." />
              <Kpi k={st === 'PARKED' ? 'Units held' : 'Allocated'} v={n(cur?.ALLOC_QTY ?? s.ALLOC_QTY)} x={`${pct(cur?.ALLOC_QTY ?? s.ALLOC_QTY, t.SZ_SRT)} of the shortage`} icon={Truck} tone="green"
                tip={st === 'PARKED' ? 'Units this parked session holds back from every other run, in whole packs.' : 'Units given, in whole packs.'} />
              <Kpi k="Packs" v={n(s.PACKS)} x={`${n(t.LINES_SERVED)} lines`} icon={Boxes} tone="indigo" />
              <Kpi k="From other RDC" v={n(s.CROSS_QTY ?? t.CROSS_QTY)} x={`${pct(s.CROSS_QTY ?? t.CROSS_QTY, s.ALLOC_QTY)} of the units`} icon={Network} tone="amber" tip="Units drawn from a warehouse other than the store’s own." />
              <Kpi k="Split lines" v={n(t.SPLIT_LINES)} x="served from 2 RDCs" icon={GitBranch} tip="Store × article lines whose packs came from both warehouses." />
            </div>
            <div><SubHead right={<span style={{ color: C.textMuted }}>checked on the stored rows before the session committed</span>}>Allocation limits</SubHead><Checks items={s.checks || []} /></div>
          </div>
        )}
      </Panel>

      <Panel pad={0}>
        <div role="tablist" style={{ display: 'flex', gap: 2, padding: '6px 10px 0', borderBottom: `1px solid ${C.cardBorder}`, overflowX: 'auto' }}>
          {tabs.map(([k, label, tip, count]) => {
            const on = curTab === k
            const c = count?.(sm)
            return (
              <Tip key={k} tip={tip}>
                <button type="button" role="tab" aria-selected={on} onClick={() => setTab(k)}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '9px 13px', fontSize: 12.5, fontWeight: 700, border: 'none',
                           background: 'none', cursor: 'pointer', whiteSpace: 'nowrap', marginBottom: -1,
                           color: on ? C.primary : C.textSub, borderBottom: `2.5px solid ${on ? C.primary : 'transparent'}` }}>
                  {label}
                  {c != null && <span style={{ fontSize: 10.5, fontWeight: 700, borderRadius: 999, padding: '1px 7px', fontVariantNumeric: 'tabular-nums',
                                               background: on ? C.primaryLt : '#f1f5f9', color: on ? C.primary : C.textMuted }}>{n(c)}</span>}
                </button>
              </Tip>
            )
          })}
        </div>
        <div style={{ padding: 14 }}>
          {curTab === 'stores' && <DataTable rows={sm.per_store || []} maxHeight={480} cols={STORE_COLS} exportName={`1SZ_ALC_${s.SESSION_ID}_per_store.csv`} />}
          {curTab === 'warehouse' && <DataTable rows={sm.by_src || []} maxHeight={260} exportName={`1SZ_ALC_${s.SESSION_ID}_by_warehouse.csv`} />}
          {curTab === 'cats' && <DataTable rows={sm.by_cat || []} maxHeight={480} empty="Nothing allocated" exportName={`1SZ_ALC_${s.SESSION_ID}_by_category.csv`} />}
          {curTab === 'rounds' && (
            <div style={{ display: 'grid', gap: 16 }}>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 16 }}>
                <div><SubHead right={<span style={{ color: C.textMuted }}>0 = the line got nothing</span>}>By the last round that shipped</SubHead>
                  <DataTable rows={sm.by_round || []} maxHeight={300} exportName={`1SZ_ALC_${s.SESSION_ID}_by_round.csv`} /></div>
                <div><SubHead right={<span style={{ color: C.textMuted }}>ADMITTED_BY_I_ROD = lines one round would not ask for</span>}>By entitlement</SubHead>
                  <DataTable rows={sm.by_irod || []} maxHeight={300} exportName={`1SZ_ALC_${s.SESSION_ID}_by_irod.csv`} /></div>
              </div>
              <div><SubHead>Real sizes vs full options</SubHead><DataTable rows={sm.by_kind || []} maxHeight={160} exportName={`1SZ_ALC_${s.SESSION_ID}_by_kind.csv`} /></div>
            </div>
          )}
          {curTab === 'lines' && <Lines sid={s.SESSION_ID} />}
          {curTab === 'settings' && <SettingsUsed s={s} wh={wh} />}
        </div>
      </Panel>
    </div>
  )
}

function Field({ k, tip, children }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '170px minmax(0, 1fr)', gap: 12, padding: '8px 0', borderTop: '1px solid #f1f5f9', alignItems: 'center' }}>
      <Tip tip={tip}><span style={{ fontSize: 12, fontWeight: 700, color: C.textSub, cursor: tip ? 'help' : 'default' }}>{k}</span></Tip>
      <span style={{ fontSize: 12.5, color: C.text, minWidth: 0, display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>{children}</span>
    </div>
  )
}

const yesNo = v => (v == null ? <span style={{ color: C.textMuted }}>not recorded</span> : v ? 'On' : 'Off')

function SettingsUsed({ s, wh }) {
  const sm = s.summary || {}
  const o = sm.options || {}
  const all = wh.cross === 'all'
  const stores = s.store_list?.stores || []
  const seq = s.store_list?.sequence
  const warns = o.readiness || sm.readiness || []
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: 20, alignItems: 'start' }}>
      <div style={{ minWidth: 0 }}>
        <SubHead>Choices</SubHead>
        <div style={{ borderBottom: '1px solid #f1f5f9' }}>
          <Field k="Warehouse" tip="Where a store could draw stock from"><WarehouseChips {...wh} /></Field>
          {all && <Field k="Protect other RDC’s stores" tip="Stock the other warehouse’s own listed stores need is held back; only its surplus moves.">{yesNo(wh.known ? wh.protect : null)}</Field>}
          {all && <Field k="One source per line" tip="A store’s line is filled from one warehouse; split only when one can’t cover it.">{yesNo(wh.known ? wh.one : null)}</Field>}
          <Field k="Store priority">{PRIORITY_WORDS[s.PRIORITY_MODE] || s.PRIORITY_MODE}{s.WEIGHT_MJ != null && <span style={{ color: C.textSub }}>w {s.WEIGHT_MJ}</span>}</Field>
          <Field k="Stock to use" tip="Every run uses the live free stock left after the parked sessions">{stockWords(s)}</Field>
          {s.ROUND_ENVELOPE && <Field k="Round scaling" tip="How the refill rounds scale the targets">{s.ROUND_ENVELOPE}</Field>}
          <Field k="Builds read" tip="The Step 1 and Step 2 builds this run read"><Chip>Step 1 #{s.BASE_BUILD_ID ?? '—'}</Chip><Chip>Step 2 #{s.REQ_BUILD_ID ?? '—'}</Chip></Field>
          <Field k="Run by">{s.CREATED_BY || '—'} · {when(s.SESSION_AT)} · {Math.round(s.DURATION_SEC || 0)} s</Field>
          {s.NOTE && <Field k="Note">“{s.NOTE}”</Field>}
          {sm.steps && <Field k="Timing (s)" tip="Seconds per stage of the run">
            <span style={{ fontSize: 11.5, color: C.textSub }}>{Object.entries(sm.steps).map(([k, v]) => `${k} ${v}`).join(' · ')}</span></Field>}
        </div>
        {warns.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <SubHead>Readiness warnings at run time</SubHead>
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12, color: C.textSub, lineHeight: 1.6 }}>
              {warns.map((w, i) => <li key={i}>{typeof w === 'string' ? w : w.detail || w.name || JSON.stringify(w)}</li>)}
            </ul>
          </div>
        )}
      </div>
      <div style={{ minWidth: 0, display: 'grid', gap: 14 }}>
        <div>
          <SubHead right={<Chip tone="green">{n(s.STORES_OK ?? stores.length)} ok of {n(s.STORES_GIVEN)}</Chip>}>Stores in the run</SubHead>
          <div style={{ maxHeight: 140, overflow: 'auto', padding: '8px 10px', borderRadius: 10, border: `1px solid ${C.cardBorder}`, background: '#f8fafc',
                        fontFamily: 'ui-monospace, monospace', fontSize: 12, color: C.text, lineHeight: 1.7, wordBreak: 'break-word' }}>
            {stores.length ? stores.join(', ') : <span style={{ color: C.textMuted }}>not recorded</span>}
          </div>
        </div>
        {seq && (
          <div>
            <SubHead>Store order given</SubHead>
            <pre style={{ margin: 0, maxHeight: 120, overflow: 'auto', padding: '8px 10px', borderRadius: 10, border: `1px solid ${C.cardBorder}`,
                          background: '#f8fafc', fontSize: 12, whiteSpace: 'pre-wrap' }}>{typeof seq === 'string' ? seq : JSON.stringify(seq)}</pre>
          </div>
        )}
        <div>
          <SubHead right={<span style={{ color: C.textMuted }}>supplied codes that did not run, with the reason</span>}>Codes not used ({(s.skipped || []).length})</SubHead>
          <DataTable rows={s.skipped || []} exportName={`1SZ_ALC_${s.SESSION_ID}_skipped.csv`} cols={[['ST_CD', 'Store'], ['STATUS', 'Why']]} maxHeight={180} empty="Every supplied code ran" />
        </div>
      </div>
    </div>
  )
}

function Lines({ sid }) {
  const [f, setF] = useState({ store: '', art: '', maj_cat: '', only: 'alloc', sort: 'store', page: 1 })
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const get = useCallback(async (q) => {
    setBusy(true)
    try {
      const p = Object.fromEntries(Object.entries(q).filter(([, v]) => v !== '' && v != null))
      const { data: r } = await sz1API.sessionLines(sid, { ...p, size: 100 }); setData(r.data)
    } catch (e) { toast.error(errText(e, 'Could not read the lines')) }
    finally { setBusy(false) }
  }, [sid])
  useEffect(() => { get({ only: 'alloc', sort: 'store', page: 1 }) }, [get])
  const go = (patch) => { const q = { ...f, ...patch }; setF(q); get(q) }
  const enter = e => { if (e.key === 'Enter') go({ page: 1 }) }
  const pages = data ? Math.max(1, Math.ceil(data.total / 100)) : 1
  return (
    <div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12, alignItems: 'center' }}>
        <Tip tip="Exact store code"><input className="input" style={{ width: 100 }} placeholder="Store" value={f.store} onKeyDown={enter} onChange={e => setF({ ...f, store: e.target.value.toUpperCase() })} /></Tip>
        <Tip tip="Variant article or generic article number"><input className="input" style={{ width: 150 }} placeholder="Article / gen art" value={f.art} onKeyDown={enter} onChange={e => setF({ ...f, art: e.target.value })} /></Tip>
        <Tip tip="Part of a category name"><input className="input" style={{ width: 160 }} placeholder="MAJ_CAT contains" value={f.maj_cat} onKeyDown={enter} onChange={e => setF({ ...f, maj_cat: e.target.value.toUpperCase() })} /></Tip>
        <Tip tip="Which lines to show"><select className="input" style={{ width: 160 }} value={f.only} onChange={e => go({ only: e.target.value, page: 1 })}>
          <option value="alloc">Got stock</option><option value="unmet">Left unmet</option><option value="cross">From other RDC</option><option value="">Every line</option>
        </select></Tip>
        <Tip tip="Order of the rows"><select className="input" style={{ width: 180 }} value={f.sort} onChange={e => go({ sort: e.target.value, page: 1 })}>
          <option value="store">Store, category, size</option><option value="alloc">Most allocated</option><option value="unmet">Most unmet</option><option value="seq">Category, size, store order</option>
        </select></Tip>
        <Btn icon={Search} onClick={() => go({ page: 1 })} busy={busy} tip="Apply the filters (or press Enter in a box)">Show</Btn>
        {data && <Chip tone="indigo">{n(data.total)} lines · {n(data.alloc_qty)} units</Chip>}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6 }}>
          <Btn icon={ChevronLeft} disabled={f.page <= 1} onClick={() => go({ page: f.page - 1 })} tip="Previous page" />
          <span style={{ fontSize: 12, color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>page {f.page} of {n(pages)}</span>
          <Btn icon={ChevronRight} disabled={f.page >= pages} onClick={() => go({ page: f.page + 1 })} tip="Next page" />
        </div>
      </div>
      <DataTable rows={data?.rows || []} maxHeight={500} exportName={`1SZ_ALC_${sid}_lines_page${f.page}.csv`} />
    </div>
  )
}
