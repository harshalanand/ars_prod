import { useCallback, useState } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { Layers, Table2, AlertTriangle, ArrowRight, Hammer, Cpu, X, ListChecks } from 'lucide-react'
import { C } from '@/theme/colors'
import { Sz1Header, Btn, Tip, n } from '@/components/sz1/Sz1Ui'
import { useSz1Access } from '@/components/sz1/sz1Access'
import Sz1BuildPage from './Sz1BuildPage'
import Sz1GapPage from './Sz1GapPage'

// 1SZ ALC — Prepare: one page, three tabs, in the order the work is done.
//   Supply (Step 1, SZ Base) → Demand (Step 2, ST/MJ/SZ req) → Gap (CONT Gap Report).
// The tabs are the existing pages rendered embedded; this page draws the one
// header and guides the order. It never starts a build by itself: every build
// writes to production, so the user presses Generate on each tab.
// Spec: public/docs/manual/sz1_alloc.md §B–C.

const TABS = [
  { key: 'supply', label: 'Supply', icon: Layers,
    title: 'Supply · SZ Base', subtitle: 'Step 1. Pick the broken-size candidates in each warehouse from ARS_MSA_TOTAL — free stock only, size-applicable categories, any enabled rule R1–R4. Sizes and options with no company CONT are a gap, kept out.' },
  { key: 'demand', label: 'Demand', icon: Table2,
    title: 'Demand · ST/MJ/SZ Req', subtitle: 'Step 2. For every listed store, how short it is per category and size. MJ_SZ_SRT is the size budget no run may exceed. Build it after Supply: it takes Supply’s category + size pairs.' },
  { key: 'gap', label: 'Gap', icon: AlertTriangle,
    title: 'Gap · CONT Gap Report', subtitle: 'Size-applicable articles whose size has no CONT % anywhere in Master_CONT_SZ (not in CO, not in any store). Recorded with every Supply build and kept out of SZ Base.' },
]

/** The tab badges: Supply #12 · Demand #11 (older) · Gap 1,234. */
function badges(status) {
  const s1 = status?.step1
  const s2 = status?.step2
  const kind = status?.running?.kind
  const gap = (s1?.summary?.gap || []).reduce((a, r) => a + (+r.ARTICLES || 0), 0)
  return {
    supply: kind === 'build1' ? { text: 'building…', tone: 'indigo' }
      : s1 ? { text: `#${s1.BUILD_ID}`, tone: 'green' } : status ? { text: 'not built', tone: 'amber' } : null,
    demand: kind === 'build2' ? { text: 'building…', tone: 'indigo' }
      : !s2 ? (status ? { text: 'not built', tone: 'amber' } : null)
      : s1 && +s2.BUILD_ID < +s1.BUILD_ID ? { text: `#${s2.BUILD_ID} · older`, tone: 'amber' } : { text: `#${s2.BUILD_ID}`, tone: 'green' },
    gap: s1?.summary?.gap ? { text: `${n(gap)} art`, tone: gap ? 'amber' : 'green' } : null,
  }
}

/**
 * tab — fixed by the route (the old /base, /req, /gap URLs, for a user without
 * the Prepare page itself); otherwise ?tab= picks it.
 */
export default function Sz1PreparePage({ tab: fixedTab }) {
  const access = useSz1Access()
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const allowed = TABS.filter(t => access.tabs[t.key])
  const want = fixedTab || params.get('tab') || 'supply'
  const cur = allowed.find(t => t.key === want) || allowed[0]
  const [status, setStatus] = useState(null)
  const [stats, setStats] = useState({})          // per tab: the key numbers the tab reports
  const [refreshKey, setRefreshKey] = useState(0)
  const [guide, setGuide] = useState(null)        // null | 'supply' | 'demand-next' | 'demand' | 'done'

  const goTab = useCallback(t => {
    if (cur && t === cur.key) return
    if (access.page && !fixedTab) setParams({ tab: t })
    else nav(access.hrefFor(t))
  }, [access, cur, fixedTab, nav, setParams])

  const onMeta = useCallback(key => m => {
    if (m.status) setStatus(m.status)
    setStats(s => ({ ...s, [key]: m.stats || [] }))
  }, [])
  const onBuilt = useCallback(step => {
    if (step === 1) setGuide('demand-next')
    else if (step === 2) setGuide(g => (g ? 'done' : g))
  }, [])

  if (!cur) {
    return <div style={{ padding: 24, fontSize: 13, color: C.textSub }}>Your role has none of the Prepare tabs (Supply, Demand, Gap).</div>
  }

  const b = badges(status)
  const tabs = allowed.map(t => ({
    key: t.key, label: t.label, icon: t.icon, badge: b[t.key],
    tip: { title: t.title, body: t.subtitle },
  }))
  const canBoth = access.tabs.supply && access.tabs.demand
  const startGuide = () => { setGuide('supply'); goTab('supply') }

  return (
    <div className="space-y-3" style={{ paddingBottom: 12 }}>
      <Sz1Header current="prepare" title={cur.title} subtitle={cur.subtitle} icon={cur.icon} status={status}
        tabs={tabs} tab={cur.key} onTab={goTab} stats={stats[cur.key] || []}
        onRefresh={() => setRefreshKey(k => k + 1)} refreshTip="Reload this tab: the latest build, the running job and the checks"
        extra={canBoth && (
          <Btn kind="primary" icon={ListChecks} onClick={startGuide}
            tip={{ title: 'Rebuild Supply → Demand', body: 'Guides you through both builds in order: Supply first, then Demand. Nothing starts by itself — you press Generate on each tab, because every build replaces a production table.' }}>
            Rebuild Supply → Demand
          </Btn>
        )} />

      <Guide stage={guide} tab={cur.key} onClose={() => setGuide(null)}
        onDemand={() => { setGuide('demand'); goTab('demand') }} onRun={() => nav('/sz1-alloc/run')} canRun={access.run} />

      {cur.key === 'supply' && <Sz1BuildPage key="supply" step={1} embedded refreshKey={refreshKey} onMeta={onMeta('supply')} goTab={goTab} onBuilt={onBuilt} />}
      {cur.key === 'demand' && <Sz1BuildPage key="demand" step={2} embedded refreshKey={refreshKey} onMeta={onMeta('demand')} goTab={goTab} onBuilt={onBuilt} />}
      {cur.key === 'gap' && <Sz1GapPage key="gap" embedded refreshKey={refreshKey} onMeta={onMeta('gap')} goTab={goTab} />}
    </div>
  )
}

/** The order guide: one slim line that says what to press next. Never starts anything. */
function Guide({ stage, tab, onClose, onDemand, onRun, canRun }) {
  if (!stage) return null
  const steps = [
    ['supply', '1', 'Supply'],
    ['demand', '2', 'Demand'],
  ]
  const at = stage === 'supply' ? 0 : stage === 'done' ? 2 : 1
  const text = {
    supply: tab === 'supply'
      ? 'Press Preview to check, tick Replace every row, then Generate now. After Supply finishes, generate Demand.'
      : 'Open the Supply tab and generate it first. After Supply finishes, generate Demand.',
    'demand-next': 'Supply finished. Now generate Demand, so each store’s size need matches the new candidates.',
    demand: 'Tick Replace every row, then Generate now. Demand reads the Supply you just built.',
    done: 'Supply and Demand are in line. You can plan a run.',
  }[stage]
  const action = stage === 'demand-next' && tab !== 'demand'
    ? <Btn kind="primary" icon={ArrowRight} onClick={onDemand} tip="Open the Demand tab. Nothing starts until you press Generate there.">Open Demand</Btn>
    : stage === 'done' && canRun
      ? <Btn kind="primary" icon={Cpu} onClick={onRun} tip="Open Plan a run">Plan a run</Btn>
      : null
  return (
    <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', padding: '8px 12px', borderRadius: 12,
                                 background: stage === 'done' ? C.greenBg : C.primaryLt, border: `1px solid ${stage === 'done' ? C.greenBd : C.primaryBd}` }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {steps.map(([k, num, word], i) => {
          const done = i < at
          const now = i === at
          return (
            <span key={k} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              {i > 0 && <ArrowRight size={13} color={C.textMuted} />}
              <span style={{ width: 20, height: 20, borderRadius: '50%', display: 'grid', placeItems: 'center', fontSize: 11, fontWeight: 800,
                             background: done ? C.green : now ? C.primary : '#fff', color: done || now ? '#fff' : C.textSub,
                             border: `1.5px solid ${done ? C.green : now ? C.primary : C.cardBorder}` }}>{done ? '✓' : num}</span>
              <span style={{ fontSize: 12.5, fontWeight: now ? 800 : 600, color: now ? C.primary : C.text }}>{word}</span>
            </span>
          )
        })}
      </div>
      <span style={{ fontSize: 12.5, color: C.text, flex: '1 1 260px', minWidth: 0, lineHeight: 1.45 }}>
        {stage !== 'done' && <Hammer size={12} style={{ verticalAlign: -1, marginRight: 5, color: C.primary }} />}{text}
      </span>
      {action}
      <Tip tip="Hide the guide">
        <button type="button" onClick={onClose} aria-label="Hide the guide"
          style={{ width: 26, height: 26, borderRadius: 8, border: `1px solid ${C.cardBorder}`, background: '#fff', cursor: 'pointer',
                   display: 'grid', placeItems: 'center', color: C.textSub }}><X size={13} /></button>
      </Tip>
    </div>
  )
}

/**
 * The old URLs (/sz1-alloc/base, /req, /gap). Someone who may open Prepare is
 * sent to its tab; someone with only that tab's page permission (e.g. only
 * PAGE_1SZ_REQ) gets the Prepare page right here, on the URL their role allows.
 */
export function Sz1PrepareLegacy({ tab }) {
  const access = useSz1Access()
  if (access.page) return <Navigate to={`/sz1-alloc/prepare?tab=${tab}`} replace />
  return <Sz1PreparePage tab={tab} />
}
