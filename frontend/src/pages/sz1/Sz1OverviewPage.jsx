import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  LayoutDashboard, Layers, Table2, ClipboardCheck, AlertTriangle, PackageOpen, Ruler, Lock, ShieldAlert,
  ArrowRight, Loader2, CheckCircle2, Hammer, ChevronRight, Settings2, BookOpen, Cpu, ParkingSquare,
} from 'lucide-react'
import { sz1API } from '@/services/api'
import { C } from '@/theme/colors'
import { Sz1Header, Panel, Kpi, Btn, Tip, Chip, Skeleton, n, when, ago, errText, MODE_WORDS, CROSS_WORDS } from '@/components/sz1/Sz1Ui'
import { StatusBadge, AgeChip, statusOf, isParked, isStale, parkedSummary, approvedToday, STALE_DAYS } from '@/components/sz1/Sz1SessionBits'
import Sz1Readiness from '@/components/sz1/Sz1Readiness'
import { useSz1Access } from '@/components/sz1/sz1Access'

// 1SZ ALC — Overview, the module's landing page: where things stand and the
// ONE thing to do next. Loads status, sessions and readiness in parallel;
// each box shows a placeholder until its own answer arrives, so one slow call
// never holds up the others. Read only — nothing here writes.
// Spec: public/docs/manual/sz1_alloc.md.

const STEP_KEYS = ['step1_built', 'step2_built', 'step2_after_step1']
const JOB_WORDS = { build1: 'Supply build', build2: 'Demand build', run: 'Run' }

/** The next thing to do, from the status, the run readiness and the parked sessions. */
function nextStep(status, ready, readyTried, sessions) {
  const s1 = status?.step1
  const s2 = status?.step2
  const items = ready?.items || []
  const blocks = items.filter(i => i.level === 'block')
  const warns = items.filter(i => i.level === 'warn')
  const live = status?.running
  if (live) {
    const tab = live.kind === 'build1' ? 'supply' : live.kind === 'build2' ? 'demand' : null
    return { tone: 'indigo', icon: Loader2, spin: true, title: `${JOB_WORDS[live.kind] || 'A job'} ${live.id} is running`,
      body: 'One 1SZ ALC job runs at a time. Watch it finish; the next step unlocks when it is done.',
      cta: tab ? { label: `Open ${tab === 'supply' ? 'Supply' : 'Demand'}`, tab } : { label: 'Open Plan a run', path: '/sz1-alloc/run' } }
  }
  if (!status && !ready) return null
  const stale = (sessions || []).filter(isStale)
  if (stale.length) {
    const oldest = stale[stale.length - 1]
    return { tone: 'amber', icon: ParkingSquare, title: 'Approve or discard parked sessions',
      body: `${stale.length} parked session${stale.length > 1 ? 's are' : ' is'} older than ${STALE_DAYS} days (oldest ${oldest.SESSION_ID}, ${ago(oldest.SESSION_AT)}) and still hold${stale.length > 1 ? '' : 's'} stock back from every other run. Approve into Pend ALC or discard.`,
      cta: { label: 'Open Sessions', path: `/sz1-alloc/sessions?id=${encodeURIComponent(oldest.SESSION_ID)}` } }
  }
  const other = blocks.find(i => !STEP_KEYS.includes(i.key))
  if (other) {
    return { tone: 'red', icon: ShieldAlert, title: 'Fix readiness first',
      body: `${other.name}: ${other.detail || 'this check blocks the run.'}`, cta: { label: 'Show the checks', scroll: true } }
  }
  if ((status && !s1) || blocks.some(i => i.key === 'step1_built')) {
    return { tone: 'amber', icon: Layers, title: 'Prepare: build Supply',
      body: 'No Supply (SZ Base) is in place yet. Build it first — it picks the broken-size candidates in each warehouse.',
      cta: { label: 'Build Supply', tab: 'supply' } }
  }
  const stale1 = warns.find(i => i.key === 'rules' || (i.group === 'freshness' && /Step 1/.test(i.detail || '')))
  if (stale1) {
    return { tone: 'amber', icon: Layers, title: 'Prepare: rebuild Supply',
      body: `${stale1.detail || 'Supply is out of date.'} Then rebuild Demand.`, cta: { label: 'Rebuild Supply', tab: 'supply' } }
  }
  if ((status && !s2) || blocks.some(i => i.key === 'step2_built' || i.key === 'step2_after_step1')
      || (s1 && s2 && +s2.BUILD_ID < +s1.BUILD_ID)) {
    return { tone: 'amber', icon: Table2, title: s2 ? 'Prepare: rebuild Demand' : 'Prepare: build Demand',
      body: s2 ? `Demand build #${s2.BUILD_ID} is older than Supply build #${s1?.BUILD_ID}. Rebuild it so the store size needs match the candidates.`
        : 'Supply is built. Now build Demand — each listed store’s shortage per category and size.',
      cta: { label: s2 ? 'Rebuild Demand' : 'Build Demand', tab: 'demand' } }
  }
  const stale2 = warns.find(i => i.group === 'freshness' && /Step 2/.test(i.detail || ''))
  if (stale2) {
    return { tone: 'amber', icon: Table2, title: 'Prepare: rebuild Demand', body: stale2.detail, cta: { label: 'Rebuild Demand', tab: 'demand' } }
  }
  if (!ready && !readyTried) return null      // the step checks are still out: don't say "ready" yet
  if (!ready) {
    return { tone: 'indigo', icon: Cpu, title: 'Plan a run',
      body: 'Supply and Demand are in line. The readiness checks could not run just now — the run checks again when it starts.',
      cta: { label: 'Plan a run', path: '/sz1-alloc/run' } }
  }
  return { tone: 'green', icon: CheckCircle2, title: 'Ready to plan a run',
    body: warns.length ? `Supply and Demand are in line. ${warns.length} warning${warns.length > 1 ? 's' : ''} below — a run may go ahead.`
      : 'Supply and Demand are in line and every check passes. Choose stores and allocate.',
    cta: { label: 'Plan a run', path: '/sz1-alloc/run' } }
}

export default function Sz1OverviewPage() {
  const nav = useNavigate()
  const access = useSz1Access()
  const [status, setStatus] = useState(null)
  const [statusErr, setStatusErr] = useState(null)
  const [sessions, setSessions] = useState(null)
  const [sessErr, setSessErr] = useState(null)
  const [ready, setReady] = useState(null)
  const [readyTried, setReadyTried] = useState(false)
  const [readyKey, setReadyKey] = useState(0)
  const readyBox = useRef(null)

  const load = useCallback(() => {
    // in parallel, each on its own: a slow status never holds up the sessions
    sz1API.status().then(({ data: r }) => { setStatus(r.data); setStatusErr(null) })
      .catch(e => setStatusErr(errText(e, 'Could not load the status')))
    sz1API.sessions(100).then(({ data: r }) => { setSessions(r.data.sessions || []); setSessErr(null) })
      .catch(e => setSessErr(errText(e, 'Could not load the sessions')))
  }, [])
  useEffect(() => { load() }, [load])
  const refresh = () => { load(); setReadyKey(k => k + 1) }

  const s1 = status?.step1
  const s2 = status?.step2
  const sumOf = (rows, k) => (rows || []).reduce((a, r) => a + (+r[k] || 0), 0)
  const gap = s1?.summary?.gap
  const ps = parkedSummary(sessions)
  const next = nextStep(status, ready, readyTried, sessions)

  const go = cta => {
    if (!cta) return
    if (cta.scroll) { readyBox.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }); return }
    if (cta.tab) { const href = access.tabs[cta.tab] ? access.hrefFor(cta.tab) : null; if (href) nav(href); return }
    if (cta.path) nav(cta.path)
  }
  const ctaAllowed = cta => !cta || cta.scroll || (cta.tab ? access.tabs[cta.tab] : cta.path === '/sz1-alloc/run' ? access.run
    : cta.path?.startsWith('/sz1-alloc/sessions') ? access.sessions : true)
  const sk = <Skeleton w={90} h={22} style={{ marginTop: 2 }} />

  return (
    <div className="space-y-3" style={{ paddingBottom: 12 }}>
      <Sz1Header current="overview" title="Overview" icon={LayoutDashboard} status={status}
        subtitle="1SZ ALC moves broken-size and residual warehouse stock to the stores short of exactly those sizes — in whole packs, never above a store’s size budget. Prepare builds Supply and Demand; Plan a run allocates; every run is a session."
        onRefresh={refresh} refreshTip="Reload the status, the sessions and the readiness checks" />

      <NextStep next={next} loading={!next && !(statusErr && readyTried)} allowed={next ? ctaAllowed(next.cta) : false} onGo={() => go(next?.cta)} />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(220px, 100%), 1fr))', gap: 10 }}>
        <Kpi k="Supply" icon={PackageOpen} tone="indigo"
          v={status ? (s1 ? `${n(s1.ROWS_BUILT)} art` : 'Not built') : statusErr ? '—' : sk}
          x={s1 ? `${n(sumOf(s1.summary?.by_rdc, 'FNL_Q'))} pcs free · #${s1.BUILD_ID} · ${ago(s1.FINISHED_AT)}` : status ? 'build it in Prepare' : ''}
          tip={{ title: 'Supply · SZ Base', body: 'Broken-size candidate articles from the latest Supply build, and their free stock (Σ FNL_Q) across both RDCs.' }} />
        <Kpi k="Store size shortage" icon={Ruler} tone="blue"
          v={status ? (s2 ? `${n(sumOf(s2.summary?.by_rdc, 'MJ_SZ_SRT'))} pcs` : 'Not built') : statusErr ? '—' : sk}
          x={s2 ? `${n(sumOf(s2.summary?.by_rdc, 'SHORT_ROWS'))} short rows · #${s2.BUILD_ID} · ${ago(s2.FINISHED_AT)}` : status ? 'build Demand in Prepare' : ''}
          tip={{ title: 'Demand · size budget', body: 'Σ MJ_SZ_SRT of the latest Demand build: how many pieces the listed stores are short, per category and size. No run may give more.' }} />
        <Kpi k="Parked" icon={Lock} tone={ps.stale ? 'amber' : ps.count ? 'indigo' : 'slate'}
          v={sessions ? `${n(ps.count)} session${ps.count === 1 ? '' : 's'}` : sessErr ? '—' : sk}
          x={sessions ? `${n(ps.units)} units held · approved today: ${n(approvedToday(sessions))}${ps.stale ? ` · ${ps.stale} older than ${STALE_DAYS} d` : ''}`
            : ''}
          tip={{ title: 'Parked sessions', body: `Units the parked sessions hold inside 1SZ: every new run starts from the live free stock after them. Approve a session into Pend ALC or discard it; one parked more than ${STALE_DAYS} days turns amber. Approved today = sessions written to ARS_PEND_ALC today.` }} />
        <Kpi k="Zero-CONT gap" icon={AlertTriangle} tone={gap && sumOf(gap, 'ARTICLES') ? 'amber' : 'slate'}
          v={status ? (gap ? `${n(sumOf(gap, 'ARTICLES'))} art` : '—') : statusErr ? '—' : sk}
          x={gap ? `${n(sumOf(gap, 'EXCLUDED'))} kept out of Supply` : status ? 'recorded with each Supply build' : ''}
          tip={{ title: 'CONT gap', body: 'Articles whose size has no CONT % anywhere in Master_CONT_SZ. Open Prepare → Gap for the lines.' }} />
      </div>
      {statusErr && <div style={{ fontSize: 12, color: C.red }}>Status: {statusErr}</div>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(440px, 100%), 1fr))', gap: 12, alignItems: 'start' }}>
        <div ref={readyBox} style={{ minWidth: 0, scrollMarginTop: 12 }}>
          <Sz1Readiness scope="run" refreshKey={readyKey} onLoaded={r => { setReady(r); setReadyTried(true) }}
            action={it => {
              const tab = it.key === 'step1_built' || /Step 1/.test(it.detail || '') ? 'supply'
                : it.key === 'step2_built' || it.key === 'step2_after_step1' || /Step 2/.test(it.detail || '') ? 'demand' : null
              return tab && access.tabs[tab]
                ? <Btn icon={Hammer} onClick={() => nav(access.hrefFor(tab))} tip={`Open Prepare → ${tab === 'supply' ? 'Supply' : 'Demand'}`}>
                    {tab === 'supply' ? 'Open Supply' : 'Open Demand'}</Btn>
                : null
            }} />
        </div>

        <Panel title="Recent sessions" icon={ClipboardCheck} pad={0} collapseKey="sessions"
          tip="The latest runs. Parked = holding stock in 1SZ; Approved = in Pend ALC."
          right={access.sessions && <Btn onClick={() => nav('/sz1-alloc/sessions')} icon={ChevronRight} tip="Open Sessions">All sessions</Btn>}>
          <SessionList rows={sessions} err={sessErr} canOpen={access.sessions} onOpen={id => nav(`/sz1-alloc/sessions?id=${encodeURIComponent(id)}`)} />
        </Panel>
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', fontSize: 12, color: C.textMuted }}>
        <span>Also:</span>
        {access.settings && <Btn icon={Settings2} onClick={() => nav('/sz1-alloc/settings')} tip="The Supply selection rules, Demand’s size share and the run defaults">Settings</Btn>}
        <Btn icon={BookOpen} onClick={() => nav('/sz1-alloc/help')} tip="How 1SZ ALC works, step by step">Help</Btn>
      </div>
    </div>
  )
}

const TONE = {
  indigo: [C.primary, C.primaryLt, C.primaryBd], green: [C.green, C.greenBg, C.greenBd],
  amber: [C.amber, C.amberBg, C.amberBd], red: [C.red, C.redBg, C.redBd],
}

/** The one call to action. */
function NextStep({ next, loading, allowed, onGo }) {
  if (!next && !loading) {
    return (
      <section style={{ padding: '14px 16px', borderRadius: 14, background: C.card, border: `1px solid ${C.cardBorder}`, fontSize: 12.5, color: C.textSub }}>
        The next step could not be worked out — the status did not load. Press refresh above to try again.
      </section>
    )
  }
  if (!next) {
    return (
      <section style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '14px 16px', borderRadius: 14, background: C.card,
                        border: `1px solid ${C.cardBorder}` }}>
        <Skeleton w={40} h={40} r={12} />
        <div style={{ flex: 1, display: 'grid', gap: 7 }}><Skeleton w="34%" h={15} /><Skeleton w="62%" h={11} /></div>
        <span style={{ fontSize: 12, color: C.textMuted, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Loader2 size={13} className="animate-spin" /> Working out the next step…
        </span>
      </section>
    )
  }
  const [fg, bg, bd] = TONE[next.tone] || TONE.indigo
  const Icon = next.icon
  return (
    <section aria-label="Next step" style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap', padding: '14px 16px', borderRadius: 14,
                                             background: `linear-gradient(90deg, ${bg} 0%, #ffffff 70%)`, border: `1px solid ${bd}`,
                                             borderLeft: `4px solid ${fg}`, boxShadow: '0 1px 3px rgba(15,23,42,.05)' }}>
      <span style={{ width: 40, height: 40, borderRadius: 12, background: fg, color: '#fff', display: 'grid', placeItems: 'center', flex: '0 0 40px' }}>
        <Icon size={20} className={next.spin ? 'animate-spin' : undefined} />
      </span>
      <div style={{ flex: '1 1 300px', minWidth: 0 }}>
        <div style={{ fontSize: 10.5, fontWeight: 800, letterSpacing: '.08em', textTransform: 'uppercase', color: fg }}>Next step</div>
        <div style={{ fontSize: 16, fontWeight: 800, color: C.text, lineHeight: 1.3 }}>{next.title}</div>
        <div style={{ fontSize: 12.5, color: C.textSub, lineHeight: 1.5, marginTop: 2 }}>{next.body}</div>
      </div>
      {next.cta && (allowed ? (
        <Btn kind="primary" icon={next.cta.scroll ? ShieldAlert : ArrowRight} onClick={onGo} tip={next.title}
          style={{ padding: '9px 16px', fontSize: 13 }}>{next.cta.label}</Btn>
      ) : (
        <Chip tone="amber">Your role cannot open this page — ask an administrator</Chip>
      ))}
    </section>
  )
}

const STATUS_WORD = { PARKED: 'parked, holding stock in 1SZ', APPROVED: 'approved into Pend ALC', DISCARDED: 'discarded', FAILED: 'failed', CANCELLED: 'cancelled', RUNNING: 'running' }

function SessionList({ rows, err, canOpen, onOpen }) {
  if (err) return <div style={{ padding: 14, fontSize: 12, color: C.red }}>{err}</div>
  if (!rows) {
    return (
      <div style={{ padding: 12, display: 'grid', gap: 10 }}>
        {[0, 1, 2, 3].map(i => <Skeleton key={i} h={34} r={8} />)}
      </div>
    )
  }
  if (!rows.length) return <div style={{ padding: 14, fontSize: 12.5, color: C.textMuted }}>No session yet. Plan a run once Supply and Demand are built.</div>
  return (
    <div>
      {rows.slice(0, 6).map((s, i) => {
        const row = (
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.3fr) auto minmax(0, 1fr) auto', alignItems: 'center', gap: 10,
                        padding: '9px 14px', borderTop: i ? `1px solid #f1f5f9` : 'none', cursor: canOpen ? 'pointer' : 'default' }}
            onClick={canOpen ? () => onOpen(s.SESSION_ID) : undefined} className={canOpen ? 'sz1-ov-row' : undefined}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 12.5, fontWeight: 700, color: C.text, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {s.SESSION_ID}
              </div>
              <div style={{ fontSize: 11, color: C.textMuted, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {ago(s.SESSION_AT)} · {s.CREATED_BY || '—'}
              </div>
            </div>
            <span style={{ display: 'flex', gap: 5, alignItems: 'center' }}>
              <StatusBadge s={s} />
              {isParked(s) && <AgeChip s={s} />}
            </span>
            <div style={{ minWidth: 0, fontSize: 11.5, color: C.textSub, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {MODE_WORDS[s.PRIORITY_MODE] || s.PRIORITY_MODE || '—'} · {CROSS_WORDS[s.CROSS_RDC] || s.CROSS_RDC || '—'}
            </div>
            <div style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
              <div style={{ fontSize: 13, fontWeight: 800, color: C.text }}>{n(s.ALLOC_QTY)}</div>
              <div style={{ fontSize: 11, color: C.textMuted }}>{n(s.STORES_SERVED)} stores</div>
            </div>
          </div>
        )
        return (
          <Tip key={s.SESSION_ID} block tip={{ title: `Session ${s.SESSION_ID}`, body: `${when(s.SESSION_AT)} · ${n(s.ALLOC_QTY)} units to ${n(s.STORES_SERVED)} of ${n(s.STORES_OK)} stores · ${STATUS_WORD[statusOf(s)] || statusOf(s)}.${canOpen ? ' Click to open.' : ''}` }}>
            {row}
          </Tip>
        )
      })}
      <style>{`.sz1-ov-row:hover { background: ${C.primaryLt} }`}</style>
    </div>
  )
}
