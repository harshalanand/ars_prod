import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Eye, Hammer, Loader2, Search, Wrench, History, BarChart3, Store, SlidersHorizontal,
  Percent, AlertTriangle, Trash2, X,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API, saveBlob } from '@/services/api'
import { confirmDialog } from '@/components/ui/ConfirmDialog'
import { C } from '@/theme/colors'
import {
  Sz1Header, Panel, Banner, Sub, DataTable, Btn, Tip, Chip, Modal, SettingLabel, n, when, errText,
} from '@/components/sz1/Sz1Ui'
import { ACTION_HELP } from '@/components/sz1/sz1Help'
import { STEP_TEXT } from '@/components/sz1/sz1StepText'
import { useSz1Access } from '@/components/sz1/sz1Access'
import Sz1RulesDialog, { rulesSummary } from '@/components/sz1/Sz1RulesPanel'
import Sz1Readiness from '@/components/sz1/Sz1Readiness'

// 1SZ ALC — Step 1 (Supply · Generate SZ Base) and Step 2 (Demand · Generate
// ST/MJ/SZ req). Rendered as a tab of Prepare (embedded: Prepare draws the one
// header and the tabs; this page reports its status + key numbers up through
// onMeta). Spec: public/docs/manual/sz1_alloc.md §B–C.
const EMPTY_FILT = { rdc: '', maj_cat: '', store: '', art: '', sz: '', rule: '', cont_src: '' }

/**
 * step — 1 (Supply) | 2 (Demand).
 * embedded — drop the page header (the Prepare page draws it); then
 *   onMeta({ status, stats }) feeds that header, refreshKey (bumped) reloads,
 *   goTab('supply' | 'demand' | 'gap') switches tab, onBuilt(step, build) fires when a build finishes.
 */
export default function Sz1BuildPage({ step, embedded = false, refreshKey = 0, onMeta, goTab, onBuilt }) {
  const P = STEP_TEXT[step]
  const nav = useNavigate()
  const access = useSz1Access()
  const toTab = t => (goTab ? goTab(t) : nav(access.hrefFor(t)))
  const metaCb = useRef(onMeta)
  metaCb.current = onMeta
  const builtCb = useRef(onBuilt)
  builtCb.current = onBuilt
  const [status, setStatus] = useState(null)
  const [job, setJob] = useState(null)
  const [preview, setPreview] = useState(null)
  const [previewing, setPreviewing] = useState(false)
  const [sure, setSure] = useState(false)
  const [history, setHistory] = useState([])
  const [filt, setFilt] = useState(EMPTY_FILT)
  const [browse, setBrowse] = useState(null)
  const [browsing, setBrowsing] = useState(false)
  const [, setExporting] = useState(false)
  const [popup, setPopup] = useState(null)          // 'rules' | 'cont'
  const [cf, setCf] = useState({})                  // column filters, applied on the server
  const [bsort, setBsort] = useState(null)          // clicked header {col, dir}, applied on the server
  const [browseTotal, setBrowseTotal] = useState(0)
  const [ready, setReady] = useState(null)          // GET /sz1/readiness result (null = not loaded / failed)
  const [readyKey, setReadyKey] = useState(0)       // bump to check readiness again
  const poll = useRef(null)

  const load = useCallback(async () => {
    try {
      const [{ data: s }, { data: h }] = await Promise.all([sz1API.status(), sz1API.builds(step)])
      setStatus(s.data); setHistory(h.data.builds || [])
      return s.data
    } catch (e) { toast.error(errText(e, 'Could not load the status')) }
  }, [step])

  const watch = useCallback((id) => {
    clearInterval(poll.current)
    const tick = async () => {
      try {
        const { data: r } = await sz1API.build(id)
        if (r.data.STATUS === 'RUNNING') { setJob(r.data); return }
        clearInterval(poll.current); setJob(null); setReadyKey(k => k + 1)
        r.data.STATUS === 'DONE' ? toast.success(`${P.tabName} built: ${n(r.data.ROWS_BUILT)} ${P.unit}`)
          : toast.error(`Build ${id} failed: ${String(r.data.ERROR || '').split('\n')[0]}`)
        if (r.data.STATUS === 'DONE') builtCb.current?.(step, r.data)
        load()
      } catch { clearInterval(poll.current) }
    }
    tick(); poll.current = setInterval(tick, 2000)
  }, [load, P.tabName, P.unit, step])

  useEffect(() => {
    setPreview(null); setBrowse(null); setSure(false); setFilt(EMPTY_FILT); setCf({}); setBsort(null); setReady(null)
    load().then(s => { if (s?.running?.kind === `build${step}`) watch(s.running.id) })
    return () => clearInterval(poll.current)
  }, [load, watch, step])

  const doPreview = async (values) => {
    setPreviewing(true)
    try { const { data: r } = await sz1API.preview(step, values); setPreview({ ...r.data, unsaved: !!values }) }
    catch (e) { toast.error(errText(e, 'Preview failed')) }
    finally { setPreviewing(false) }
  }
  const doBuild = async () => {
    try { const { data: r } = await sz1API.startBuild(step); toast(r.message); setSure(false); watch(r.data.build_id) }
    catch (e) { toast.error(errText(e, 'Could not start the build')) }
  }
  const params = () => Object.fromEntries(Object.entries(filt).filter(([, v]) => v))
  const cfParam = f => (Object.keys(f).length ? { cf: JSON.stringify(f) } : {})
  const sortParam = o => (o ? { ob: o.col, od: o.dir } : {})
  const doBrowse = async (f = cf, o = bsort) => {
    setBrowsing(true)
    try {
      const { data: r } = await sz1API.rows(step, { ...params(), ...cfParam(f), ...sortParam(o), top: 2000 })
      setBrowse(r.data.rows); setBrowseTotal(r.data.total ?? r.data.rows.length)
    }
    catch (e) { toast.error(errText(e, 'Could not read the rows')) }
    finally { setBrowsing(false) }
  }
  // the table's column filters: re-read from the server so they search the WHOLE table
  const tableFilter = {
    filters: cf, total: browseTotal, sort: bsort,
    onChange: f => { setCf(f); doBrowse(f) },
    onSort: o => { setBsort(o); doBrowse(cf, o) },
    onExport: () => doExport(),
    distinct: async (col, search, others) => {
      const { data: r } = await sz1API.distinct(step === 1 ? 'base' : 'req', { ...params(), ...cfParam(others), col, search: search || undefined })
      return r.data
    },
  }
  const filtered = Object.values(filt).some(v => v) || Object.keys(cf).length > 0
  const doExport = async () => {
    setExporting(true)
    try { const res = await sz1API.exportBuilt(step, { ...params(), ...cfParam(cf), ...sortParam(bsort) }); toast.success(`Saved ${saveBlob(res, `1SZ_ALC_step${step}.csv`)}`) }
    catch (e) { toast.error(errText(e, 'Export failed')) }
    finally { setExporting(false) }
  }
  const repair = async () => {
    try { const { data: r } = await sz1API.repairBalances(); toast.success(r.message); load() }
    catch (e) { toast.error(errText(e, 'Could not re-apply balances')) }
  }
  const clearHistory = async () => {
    const ok = await confirmDialog({ tone: 'warn', title: `Clear the ${P.step} build history?`, confirmLabel: 'Clear',
      body: 'The older build-history rows are removed. The latest successful build is kept (the page, the settings check and Step 3 read it), and so is a build still running.',
      bullets: ['The built table itself is not touched.'] })
    if (!ok) return
    try { const { data: r } = await sz1API.clearBuilds(step); toast.success(r.message); load() }
    catch (e) { toast.error(errText(e, 'Could not clear the history')) }
  }

  const built = status?.[`step${step}`]
  const sm = built?.summary || {}
  const otherJob = status?.running && status.running.kind !== `build${step}`
  const blockedBy1 = step === 2 && status && !status.step1
  const byRdc = sm.by_rdc || []
  const sum = k => byRdc.reduce((a, r) => a + (+r[k] || 0), 0)
  const gapExcl = (sm.gap || []).reduce((a, r) => a + (+r.EXCLUDED || 0), 0)
  const gapAll = (sm.gap || []).reduce((a, r) => a + (+r.ARTICLES || 0), 0)
  const set = (k, v) => setFilt(f => ({ ...f, [k]: v }))
  const readyBlock = ready?.block || []
  const refresh = () => { load(); setReadyKey(k => k + 1) }
  const mountKey = useRef(refreshKey)            // a bump from the Prepare header reloads; mounting already loads
  useEffect(() => { if (refreshKey !== mountKey.current) refresh() }, [refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const b1 = status?.step1
  const demandBehind = step === 2 && b1 && built && +built.BUILD_ID < +b1.BUILD_ID

  const stats = built ? [
          { k: P.unit === 'articles' ? 'Articles' : 'Rows', v: n(built.ROWS_BUILT), tip: `Rows in ${P.table} now.` },
          step === 1 ? { k: 'Free stock', v: n(sum('FNL_Q')), x: 'pcs, both RDCs', tip: 'Σ FNL_Q of the candidates.', tone: 'indigo' }
                     : { k: 'Size shortage', v: n(sum('MJ_SZ_SRT')), x: `${n(sum('SHORT_ROWS'))} short rows`, tip: 'Σ MJ_SZ_SRT — the total size budget.', tone: 'indigo' },
          ...(step === 1 && sm.gap ? [{ k: 'CONT gap', v: n(gapAll), x: `${n(gapExcl)} kept out`, tip: 'Articles with 0 company CONT — see the CONT Gap Report.', tone: 'amber' }] : []),
          { k: 'Build', v: `#${built.BUILD_ID}`, x: built.CREATED_BY, tip: 'Logged in ARS_SZ1_BUILD with its summary.' },
          { k: 'Built', v: when(built.FINISHED_AT), x: `${Math.round(built.DURATION_SEC || 0)} s` },
        ] : [{ k: 'Status', v: 'Not built yet', x: `Preview, then Generate to fill ${P.table}`, tone: 'amber' }]
  // the Prepare header shows these numbers; they change only with the status
  useEffect(() => { if (status) metaCb.current?.({ status, stats }) }, [status]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-3" style={{ paddingBottom: 12 }}>
      {!embedded && (
        <Sz1Header current={P.key} title={P.title} subtitle={P.subtitle} icon={P.icon} status={status}
          onRefresh={refresh} refreshTip="Reload the latest build, the running job and the readiness checks" stats={stats} />
      )}

      <Sz1Readiness scope={step === 1 ? 'build1' : 'build2'} refreshKey={readyKey} onLoaded={setReady}
        action={it => (step === 2 && it.level === 'block' && /step ?1|build1|SZ Base/i.test(`${it.key} ${it.name} ${it.detail}`)
          ? <Btn kind="primary" icon={Hammer} onClick={() => toTab('supply')} tip="Open the Supply tab">Build Supply first</Btn> : null)} />
      {(status?.drift || []).length > 0 && (
        <Banner tone="amber" label="Balances" text={`Out of step with the recorded allocation — ${status.drift.join('; ')}.`}
          action={<Btn icon={Wrench} onClick={repair} tip={ACTION_HELP.repair}>Re-apply balances</Btn>} />
      )}
      {blockedBy1 && !ready && <Banner tone="red" label="First" text="Build Supply (SZ Base) first — Demand takes its MAJ_CAT + size pairs from it."
        action={<Btn icon={Hammer} onClick={() => toTab('supply')} tip="Open the Supply tab">Open Supply</Btn>} />}
      {demandBehind && !job && (
        <Banner tone="amber" label="Order" text={`Demand build #${built.BUILD_ID} is older than Supply build #${b1.BUILD_ID}. Generate Demand now so the store size needs match the new candidates.`} />
      )}

      {/* One row: the build controls, then the settings labels that open popups. */}
      <Panel title="Build" icon={Hammer} accent={C.primary} tip={ACTION_HELP.build} pad={12}
        right={built && <span>holds build <b style={{ color: C.text }}>#{built.BUILD_ID}</b> · {n(built.ROWS_BUILT)} {P.unit} · {when(built.FINISHED_AT)}</span>}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <Btn icon={Eye} onClick={() => doPreview()} busy={previewing} disabled={previewing || blockedBy1}
            tip={step === 1 ? 'Runs the selection with the SAVED rule values (unsaved ones: Preview in Selection rules). Never writes.' : ACTION_HELP.preview}>Preview</Btn>
          <Tip tip="Required: a build replaces every row of the table.">
            <label style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, padding: '6px 10px', borderRadius: 10, whiteSpace: 'nowrap',
                            background: sure ? C.primaryLt : '#f8fafc', border: `1px solid ${sure ? C.primary : C.cardBorder}`, cursor: 'pointer' }}>
              <input type="checkbox" checked={sure} onChange={e => setSure(e.target.checked)} /> Replace every row
            </label>
          </Tip>
          <Btn kind="primary" icon={Hammer} onClick={doBuild} disabled={!sure || !!job || otherJob || blockedBy1 || readyBlock.length > 0}
            tip={otherJob ? 'Another 1SZ ALC job is running — wait for it.'
              : readyBlock.length ? { title: 'Cannot generate yet', body: readyBlock.join(' · ') } : ACTION_HELP.build}>Generate now</Btn>
          {job && <Chip tone="indigo"><Loader2 size={11} className="animate-spin" style={{ verticalAlign: -1 }} /> build #{job.BUILD_ID}: {job.PROGRESS || 'running'}</Chip>}
          {otherJob && !job && <Chip tone="amber">{status.running.kind === 'run' ? 'session' : 'build'} {status.running.id} running</Chip>}
          <span style={{ width: 1, alignSelf: 'stretch', background: C.cardBorder, margin: '0 4px' }} />
          {step === 1 && (
            <SettingLabel icon={SlidersHorizontal} label="Selection rules" summary={rulesSummary(status?.rules)} onClick={() => setPopup('rules')}
              tip="Open the rule settings: switch R1–R4 and the zero-CONT gap on or off and set their values. Preview, save, reset or load the tool's rules there." />
          )}
          <SettingLabel icon={Percent} label={P.rulesTitle} summary={P.rulesSummary}
            onClick={() => setPopup('cont')} tone="blue" tip="Open the exact rule the build applies, with an example." />
          {step === 1 && (
            <SettingLabel icon={AlertTriangle} label="CONT Gap Report" summary={sm.gap ? `${n(gapAll)} articles · ${n(gapExcl)} kept out` : 'zero-CONT sizes and options'}
              onClick={() => toTab('gap')} tone="amber" tip="Sizes and whole options with no company CONT — recorded with every build, kept out of SZ Base. Opens the Gap tab." />
          )}
        </div>
      </Panel>

      {step === 1 && (
        <Sz1RulesDialog open={popup === 'rules'} onClose={() => setPopup(null)} onPreview={doPreview} previewing={previewing}
          impact={preview?.by_rule} onSaved={s => setStatus(st => (st ? { ...st, rules: s } : st))} />
      )}
      <Modal open={popup === 'cont'} onClose={() => setPopup(null)} title={P.rulesTitle} icon={Percent} width={860}>
        <DataTable rows={P.rules} maxHeight={320} toolbar={false} sortable={false} filterable={false} />
        <div style={{ fontSize: 12, color: C.textSub, marginTop: 10, lineHeight: 1.55 }}>{P.rulesNote}</div>
        {P.example && (
          <div style={{ marginTop: 12 }}>
            <Sub>{P.exampleTitle}</Sub>
            <DataTable rows={P.example} maxHeight={200} toolbar={false} sortable={false} filterable={false} />
          </div>
        )}
      </Modal>

      {preview && (
        <Panel title="Preview — nothing written" icon={Eye}
          right={<>{preview.unsaved && <Chip tone="amber">with the form’s values (not saved)</Chip>}<Chip tone="blue">first {preview.sample?.length || 0} rows</Chip>
            <Btn icon={X} onClick={() => setPreview(null)} tip="Close the preview" /></>}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 16 }}>
            <div><Sub>Per RDC</Sub><DataTable rows={preview.by_rdc} maxHeight={170} exportName={`1SZ_${P.key}_preview_by_rdc.csv`} /></div>
            {step === 1 && preview.by_rule && <div><Sub tip="Articles each rule admits, and the zero-CONT gap.">Per rule</Sub><DataTable rows={preview.by_rule} maxHeight={200} exportName="1SZ_base_preview_by_rule.csv" /></div>}
            {step === 1 && <div><Sub tip="The population the rules look at.">Live articles</Sub><DataTable rows={preview.live} maxHeight={170} exportName="1SZ_base_preview_live.csv" /></div>}
          </div>
          <Sub>Sample</Sub>
          <DataTable rows={preview.sample} maxHeight={340} exportName={`1SZ_${P.key}_preview_sample.csv`} />
        </Panel>
      )}

      {built && (
        <Panel title={`What build #${built.BUILD_ID} wrote`} icon={BarChart3}
          right={built.summary?.balance_sessions?.length ? <Chip tone="indigo">parked at build: {built.summary.balance_sessions.join(', ')}</Chip> : <Chip>no parked session at build</Chip>}>
          {step === 1 ? (
            <div style={{ display: 'grid', gap: 12 }}>
              {sm.rules ? (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                  <span style={{ fontSize: 12, color: C.textSub, fontWeight: 600 }}>Built with:</span>
                  <Chip tone="indigo">{rulesSummary(sm.rules)}</Chip>
                </div>
              ) : <div style={{ fontSize: 12, color: C.amber }}>This build predates the rule settings (tool rules). Generate again to apply the saved values.</div>}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 16 }}>
                <div><Sub>By RDC</Sub><DataTable rows={sm.by_rdc || []} maxHeight={220} exportName="1SZ_base_by_rdc.csv" /></div>
                <div><Sub tip="Which rules admitted each article.">By rule combination</Sub><DataTable rows={sm.by_rules || []} maxHeight={220} exportName="1SZ_base_by_rules.csv" /></div>
                {sm.by_cont_src && <div><Sub tip="Where each article's CONT % came from, and the option CONT % range.">By CONT source</Sub><DataTable rows={sm.by_cont_src} maxHeight={220} exportName="1SZ_base_by_cont_src.csv" /></div>}
                {sm.gap && <div><Sub tip="Zero-CONT articles of this build; EXCLUDED = kept out of SZ Base.">CONT gap</Sub><DataTable rows={sm.gap} maxHeight={220} exportName="1SZ_base_cont_gap.csv" /></div>}
                <div><Sub tip="Live sizes left in the option.">By live sizes</Sub><DataTable rows={sm.by_var_count || []} maxHeight={220} exportName="1SZ_base_by_live_sizes.csv" /></div>
              </div>
            </div>
          ) : (
            <div style={{ display: 'grid', gap: 16 }}>
              <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 3fr) minmax(0, 2fr)', gap: 16 }}>
                <div><Sub>By RDC</Sub><DataTable rows={sm.by_rdc || []} maxHeight={200} exportName="1SZ_req_by_rdc.csv" /></div>
                <div><Sub tip="How many rows took their share from each rule.">Where the size share came from</Sub><DataTable rows={sm.by_cont_src || []} maxHeight={200} exportName="1SZ_req_by_cont_src.csv" /></div>
              </div>
              {sm.totals && <div><Sub>Measure totals</Sub><DataTable rows={[sm.totals]} maxHeight={80} sortable={false} filterable={false} exportName="1SZ_req_totals.csv" /></div>}
            </div>
          )}
        </Panel>
      )}

      {built && (
        <Panel title={`Browse ${P.table}`} icon={Search}
          right={<>
            <span>sort, filter and export cover the whole table · first 2,000 rows on screen</span>
          </>}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12, alignItems: 'center' }}>
            <Tip tip="Exact warehouse code"><select className="input" style={{ width: 100 }} value={filt.rdc} onChange={e => set('rdc', e.target.value)}>
              <option value="">All RDCs</option>{['DH24', 'DW01'].map(r => <option key={r} value={r}>{r}</option>)}
            </select></Tip>
            <Tip tip="Part of a category name"><input className="input" style={{ width: 170 }} placeholder="MAJ_CAT contains" value={filt.maj_cat} onChange={e => set('maj_cat', e.target.value.toUpperCase())} /></Tip>
            {step === 1 && <Tip tip="Variant or generic article number"><input className="input" style={{ width: 140 }} placeholder="Article / gen art" value={filt.art} onChange={e => set('art', e.target.value)} /></Tip>}
            {step === 2 && <Tip tip="Exact store code"><input className="input" style={{ width: 100 }} placeholder="Store" value={filt.store} onChange={e => set('store', e.target.value.toUpperCase())} /></Tip>}
            <Tip tip="Exact size, e.g. M, 32, A"><input className="input" style={{ width: 80 }} placeholder="Size" value={filt.sz} onChange={e => set('sz', e.target.value.toUpperCase())} /></Tip>
            {step === 1 && <Tip tip="Articles a rule admitted"><select className="input" style={{ width: 110 }} value={filt.rule} onChange={e => set('rule', e.target.value)}>
              <option value="">Any rule</option>{['R1', 'R2', 'R3', 'R4'].map(r => <option key={r} value={r}>{r}</option>)}
            </select></Tip>}
            <Tip tip="Where the CONT % came from"><select className="input" style={{ width: 160 }} value={filt.cont_src} onChange={e => set('cont_src', e.target.value)}>
              <option value="">Any CONT source</option>
              {(step === 1 ? ['CO', 'STORE_AVG', 'ZERO_IN_MASTER', 'NOT_IN_MASTER'] : ['FULL_OPT', 'STORE', 'STORE_SIZE_MISSING', 'CO', 'CO_SIZE_MISSING', 'NO_CURVE', 'EVEN_N'])
                .map(c => <option key={c} value={c}>{c}</option>)}
            </select></Tip>
            <Btn icon={Store} onClick={() => doBrowse()} busy={browsing} tip="Read the built table with these filters (and the column filters). Read only.">Show rows</Btn>
            {filtered && <Btn icon={X} onClick={() => { setFilt(EMPTY_FILT); setCf({}); setBsort(null); setBrowse(null) }} tip="Clear every filter">Clear</Btn>}
          </div>
          {browse && <DataTable rows={browse} maxHeight={460} server={tableFilter} empty="No row matches" />}
        </Panel>
      )}

      <Panel title="Build history" icon={History}
        right={history.length > 1 && <Btn icon={Trash2} onClick={clearHistory} tip="Remove the older build-history rows; the latest successful build is kept.">Clear history</Btn>}>
        <DataTable rows={history} exportName={`1SZ_${P.key}_build_history.csv`} cols={[['BUILD_ID', 'Build'], ['STATUS', 'Status'], ['ROWS_BUILT', 'Rows'], ['CREATED_BY', 'By'],
          ['STARTED_AT', 'Started'], ['DURATION_SEC', 'Seconds'], ['ERROR', 'Error']]} maxHeight={240} empty="No builds yet" />
      </Panel>
    </div>
  )
}
