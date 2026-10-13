import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Play, Hammer, Cpu, History, ArrowLeft, ArrowRight, FlaskConical } from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API } from '@/services/api'
import { C } from '@/theme/colors'
import { Sz1Header, Panel, DataTable, ActionBar, Chip, Btn, Tip, n, errText, CROSS_WORDS } from '@/components/sz1/Sz1Ui'
import { ACTION_HELP } from '@/components/sz1/sz1Help'
import Sz1Readiness from '@/components/sz1/Sz1Readiness'
import { WIZ_STEPS, MODE_NAME, StepRail, StoresStep, OptionsStep, liveStockText, LIVE_STOCK_HELP } from '@/components/sz1/Sz1RunWizard'
import { PreviewStep } from '@/components/sz1/Sz1RunPreview'
import { RunStep } from '@/components/sz1/Sz1RunResult'
import { WarehouseChips, variantKey, parkedSummary, statusOf, STATUS_META } from '@/components/sz1/Sz1SessionBits'

// 1SZ ALC — Step 3 "Plan a run": a 4-step wizard (Stores → Options → Preview → Run).
// Spec: public/docs/manual/sz1_alloc.md §D. API: GET /sz1/run, POST /sz1/stores/validate,
// /sz1/stores/parse, /sz1/run/simulate (writes nothing), /sz1/run/preview, /sz1/run.
// Every run uses the live free stock after the parked sessions and ends PARKED (P1–P3);
// stock_basis is sent as 'balance' for older backends — the server treats every run as live.

const OWN = { key: 'own', cross_rdc: 'same', protect_other: true, one_source: true }
const ALL = { key: 'all', cross_rdc: 'all', protect_other: true, one_source: true }

/** The chosen warehouse option first, then the other mode; an All RDC choice that is not the default also gets the default. */
function variantsFor(f) {
  if (f.cross_rdc !== 'all') return [OWN, ALL]
  const mine = { cross_rdc: 'all', protect_other: !!f.protect_other, one_source: !!f.one_source }
  mine.key = variantKey(mine)
  return mine.key === 'all' ? [ALL, OWN] : [mine, ALL, OWN]
}

export default function Sz1RunPage() {
  const nav = useNavigate()
  const [page, setPage] = useState(null)
  const [text, setText] = useState('')
  const [file, setFile] = useState(null)
  const [checked, setChecked] = useState(null)
  const [form, setForm] = useState({ mode: 'weighted', weight_mj: 0.5, sequence: '', stock_basis: 'balance',
                                     cross_rdc: 'all', protect_other: true, one_source: true, note: '' })
  const [step, setStep] = useState(1)
  const [reach, setReach] = useState(1)               // furthest step opened
  const [sim, setSim] = useState(null)                 // { data, key, variants }
  const [simBusy, setSimBusy] = useState(false)
  const [simErr, setSimErr] = useState(null)
  const [elapsed, setElapsed] = useState(0)
  const [busy, setBusy] = useState('')
  const [job, setJob] = useState(null)
  const [shown, setShown] = useState(null)
  const [readyKey, setReadyKey] = useState(0)          // bump to check readiness again
  const [parked, setParked] = useState(null)           // { count, units } of the parked sessions
  const poll = useRef(null)
  const fileRef = useRef(null)
  const abort = useRef(null)

  const load = useCallback(async () => {
    sz1API.sessions(200).then(({ data: r }) => setParked(parkedSummary(r.data.sessions))).catch(() => setParked(null))
    try { const { data: r } = await sz1API.runPage(); setPage(r.data); return r.data }
    catch (e) { toast.error(errText(e, 'Could not load the run page')) }
  }, [])

  const watch = useCallback((id) => {
    clearInterval(poll.current)
    const tick = async () => {
      try {
        const { data: r } = await sz1API.runSession(id)
        if (r.data.STATUS === 'RUNNING') { setJob(r.data); return }
        clearInterval(poll.current); setJob(null); setShown(r.data); setReadyKey(k => k + 1)
        const msg = r.data.progress?.text || r.data.STATUS
        const st = statusOf(r.data)
        st === 'PARKED' ? toast.success(`Session ${id} parked: ${msg}`) : st === 'CANCELLED' ? toast(msg) : toast.error(msg)
        load()
      } catch { clearInterval(poll.current) }
    }
    tick(); poll.current = setInterval(tick, 1500)
  }, [load])

  useEffect(() => {
    load().then(d => { if (d?.running?.kind === 'run') { setStep(4); setReach(4); watch(d.running.id) } })
    return () => { clearInterval(poll.current); abort.current?.abort() }
  }, [load, watch])

  useEffect(() => {                                    // seconds counter while the preview works
    if (!simBusy) return undefined
    const t0 = Date.now(); setElapsed(0)
    const t = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000)
    return () => clearInterval(t)
  }, [simBusy])

  // ── stores ──
  const ok = (checked || []).filter(s => s.STATUS === 'ok').map(s => s.ST_CD)
  const skipped = (checked || []).filter(s => s.STATUS !== 'ok')
  const okRdc = (checked || []).filter(s => s.STATUS === 'ok').reduce((a, s) => ({ ...a, [s.RDC]: (a[s.RDC] || 0) + 1 }), {})

  const check = async (basis = form.stock_basis) => {
    setBusy('check')
    try {
      const { data: r } = await sz1API.validateStores(file ? file.codes.join(' ') : text, basis)
      setChecked(r.data.stores)
      if (!r.data.codes.length) toast.error('No store codes found')
    } catch (e) { toast.error(errText(e, 'Could not check the stores')) }
    finally { setBusy('') }
  }
  const pickFile = async (f, column) => {
    if (!f) return
    const fd = new FormData(); fd.append('file', f); if (column) fd.append('column', column)
    try {
      const { data: r } = await sz1API.parseStores(fd)
      setFile({ ...r.data, name: f.name, raw: f }); setChecked(null)
      toast.success(`${r.data.codes.length} store code(s) read from column ${r.data.used}`)
    } catch (e) { toast.error(errText(e, 'Could not read the file')) }
  }

  // ── options ──
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const optsOk = form.mode !== 'sequence' || !!form.sequence.trim()

  // ── preview (simulate) ──
  const runBody = {
    codes: ok, mode: form.mode, weight_mj: +form.weight_mj,
    sequence: form.mode === 'sequence' ? form.sequence : null, stock_basis: form.stock_basis,
  }
  const simKey = JSON.stringify(runBody)
  const planned = variantsFor(form)
  const chosenKey = variantKey(form)
  const simFresh = !!sim && sim.key === simKey
  const simChosen = simFresh ? sim.data?.variants?.find(v => v.key === chosenKey) : null

  const simulate = async () => {
    abort.current?.abort()
    const ctl = new AbortController(); abort.current = ctl
    setSimBusy(true); setSimErr(null)
    try {
      const { data: r } = await sz1API.simulate({ ...runBody, variants: planned }, ctl.signal)
      setSim({ data: r.data, key: simKey })
    } catch (e) {
      if (ctl.signal.aborted) toast('Stopped waiting for the preview')
      else setSimErr(errText(e, 'The preview failed'))
    } finally { if (abort.current === ctl) { abort.current = null; setSimBusy(false) } }
  }
  const stopSim = () => { abort.current?.abort(); abort.current = null; setSimBusy(false) }
  const pickVariant = v => {
    setForm(f => ({ ...f, cross_rdc: v.cross_rdc, protect_other: v.protect_other !== false, one_source: v.one_source !== false }))
    toast.success(`Switched to ${v.cross_rdc === 'all' ? 'All RDC' : 'Own RDC'}`)
  }

  // ── run ──
  const run = async () => {
    setBusy('run')
    try {
      const { data: r } = await sz1API.startRun({ ...runBody, cross_rdc: form.cross_rdc, protect_other: form.protect_other,
                                                  one_source: form.one_source, note: form.note || null })
      toast(r.message); setShown(null); watch(r.data.session_id)
    } catch (e) { toast.error(errText(e, 'Could not start the run')) }
    finally { setBusy('') }
  }
  const cancel = async () => { if (job) { try { const { data: r } = await sz1API.cancelRun(job.SESSION_ID); toast(r.message) } catch { /* ignore */ } } }

  // ── wizard navigation ──
  const allowed = s => s === 1 || (ok.length > 0 && (s === 2 || optsOk))
  const go = s => { if (allowed(s) && s <= reach + 1) { setStep(s); setReach(r => Math.max(r, s)) } }
  const finished = s => (s === 1 ? ok.length > 0 : s === 2 ? ok.length > 0 && optsOk : s === 3 ? simFresh : !!shown)
  const stepState = s => (!allowed(s) || s > reach + 1 ? 'locked' : finished(s) && s <= reach ? 'done' : 'open')
  const nextBlock = step === 1 ? (!checked ? 'Check the stores first.' : !ok.length ? 'No store is ok. Fix the list and check again.' : null)
    : step === 2 ? (!ok.length ? 'No store is ok on this stock basis. Go back to Stores.' : !optsOk ? 'My order needs a store sequence.' : null) : null

  const gates = page?.gates
  const canRun = gates?.ok && ok.length > 0 && optsOk && !job && !busy
  const why = !gates?.ok ? { title: 'Cannot run', body: (gates?.block || []).join(' · ') || 'See Readiness at the top of the page.' }
    : !ok.length ? 'Check the stores first: at least one must be ok.' : !optsOk ? 'My order needs a store sequence.' : ACTION_HELP.run
  const lastSession = page?.sessions?.[0]
  const refresh = () => { load(); setReadyKey(k => k + 1) }
  const cur = WIZ_STEPS[step - 1]

  return (
    <div className="space-y-3" style={{ paddingBottom: 4 }}>
      <Sz1Header current="run" title="Plan a run" icon={Cpu}
        subtitle="Choose stores and options, compare Own RDC with All RDC, then allocate whole packs, round by round. Every run uses the live free stock after the parked sessions and parks as a new session; approve it in Sessions."
        status={page && { step1: page.step1, step2: page.step2, running: page.running, balance_sessions: page.balance_sessions }}
        onRefresh={refresh} refreshTip="Reload the readiness checks, builds and recent sessions"
        stats={[
          { k: 'Step 1 candidates', v: n(page?.step1?.ROWS_BUILT), x: page?.step1 ? `build #${page.step1.BUILD_ID}` : 'not built', tip: 'Articles in ARS_SZ1_MSA_SZ_BASE.' },
          { k: 'Step 2 rows', v: n(page?.step2?.ROWS_BUILT), x: page?.step2 ? `build #${page.step2.BUILD_ID}` : 'not built', tip: 'Store × category × size rows in ARS_SZ1_ST_MJ_SZ_REQ_BASE.' },
          { k: 'Latest session', v: lastSession?.SESSION_ID || '—', x: lastSession ? `${STATUS_META[statusOf(lastSession)]?.label || lastSession.STATUS} · ${n(lastSession.ALLOC_QTY)} units` : 'none yet', tip: 'The newest run.' },
        ]} />

      <Sz1Readiness scope="run" refreshKey={readyKey}
        action={it => {
          const t = `${it.key} ${it.name} ${it.detail}`
          const one = /step ?1|build1|SZ Base/i.test(t)
          return it.level === 'block' && (one || /step ?2|build2|ST\/MJ\/SZ/i.test(t))
            ? <Btn kind="primary" icon={Hammer} onClick={() => nav(one ? '/sz1-alloc/prepare?tab=supply' : '/sz1-alloc/prepare?tab=demand')} tip="Open that step">Build it</Btn> : null
        }} />

      <section style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 14, boxShadow: '0 1px 3px rgba(15,23,42,.05)', minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap', padding: '10px 14px', borderBottom: `1px solid ${C.cardBorder}` }}>
          <div style={{ flex: '1 1 480px', minWidth: 0, overflowX: 'auto' }}><StepRail current={step} state={stepState} onGo={go} /></div>
          <div style={{ fontSize: 12, color: C.textSub, flex: '0 1 340px', minWidth: 0 }}>{cur.tip}</div>
        </div>
        <div style={{ padding: 16 }}>
          {step === 1 && (
            <StoresStep text={text} onText={v => { setText(v); setChecked(null) }} file={file} fileRef={fileRef}
              onClearFile={() => { setFile(null); setChecked(null) }} onPickFile={f => pickFile(f)} onColumn={c => pickFile(file.raw, c)}
              checked={checked} ok={ok} skipped={skipped} okRdc={okRdc} onCheck={() => check()} busy={busy === 'check'} maxStores={page?.max_stores} />
          )}
          {step === 2 && <OptionsStep form={form} set={set} okCount={ok.length} parked={parked} />}
          {step === 3 && (
            <PreviewStep sim={sim} fresh={simFresh} busy={simBusy} elapsed={elapsed} error={simErr} planned={planned} chosenKey={chosenKey}
              okCount={ok.length} onSimulate={simulate} onStop={stopSim} onUse={pickVariant}
              candidateBody={{ codes: ok, stock_basis: form.stock_basis, cross_rdc: form.cross_rdc }} />
          )}
          {step === 4 && (
            <RunStep job={job} shown={shown} onCancel={cancel} onOpen={id => nav(`/sz1-alloc/sessions?id=${id}`)}
              onNew={() => { setShown(null); setStep(ok.length ? 2 : 1) }} simChosen={simChosen}
              form={form} setNote={v => set('note', v)} okCount={ok.length} okRdc={okRdc} skippedCount={skipped.length}
              gates={gates} onEdit={go} parked={parked} />
          )}
        </div>
      </section>

      <Panel title="Recent sessions" icon={History} pad={12}
        right={<a href="/sz1-alloc/sessions" onClick={e => { e.preventDefault(); nav('/sz1-alloc/sessions') }} style={{ color: C.primary, fontWeight: 600 }}>All sessions →</a>}>
        <DataTable rows={(page?.sessions || []).map(s => ({ ...s, CROSS_RDC: CROSS_WORDS[s.CROSS_RDC] || s.CROSS_RDC,
                                                           PRIORITY_MODE: MODE_NAME[s.PRIORITY_MODE] || s.PRIORITY_MODE,
                                                           STATUS: STATUS_META[statusOf(s)]?.label || s.STATUS }))}
          exportName="1SZ_ALC_recent_sessions.csv" onRow={s => nav(`/sz1-alloc/sessions?id=${s.SESSION_ID}`)}
          cols={[['SESSION_ID', 'Session'], ['SESSION_AT', 'When'], ['STATUS', 'Status'], ['CROSS_RDC', 'Warehouse'], ['PRIORITY_MODE', 'Priority'],
            ['STORES_OK', 'Stores'], ['ALLOC_QTY', 'Units'], ['PACKS', 'Packs'], ['CREATED_BY', 'By']]}
          maxHeight={200} empty="No sessions yet" />
      </Panel>

      <ActionBar>
        <Btn icon={ArrowLeft} onClick={() => go(step - 1)} disabled={step === 1 || !!job} tip={step > 1 ? `Back to ${WIZ_STEPS[step - 2].name}` : 'This is the first step'}>Back</Btn>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', minWidth: 0, flex: '1 1 300px' }}>
          <Chip tone={ok.length ? 'green' : 'slate'}>{n(ok.length)} store{ok.length === 1 ? '' : 's'}</Chip>
          <WarehouseChips cross={form.cross_rdc} protect={form.protect_other} one={form.one_source} />
          <Chip>{MODE_NAME[form.mode]}{form.mode === 'weighted' ? ` ${(+form.weight_mj).toFixed(2)}` : ''}</Chip>
          <Tip tip={{ ...LIVE_STOCK_HELP, body: `${liveStockText(parked)} ${LIVE_STOCK_HELP.body}` }}>
            <Chip tone={parked?.count ? 'amber' : 'slate'}>Live stock{parked?.count ? ` · ${n(parked.count)} parked` : ''}</Chip>
          </Tip>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          {step === 3 && (sim || simBusy) && (
            <Btn icon={FlaskConical} onClick={simulate} busy={simBusy} disabled={simBusy || !ok.length}
              tip="Run each warehouse option in memory again with the current choices. Writes nothing.">Compare again</Btn>
          )}
          {step < 4 && (
            <Btn kind="primary" icon={ArrowRight} onClick={() => go(step + 1)} disabled={!!nextBlock}
              tip={nextBlock || (step === 3 && !simFresh ? 'Go to Run without a preview. You can come back.' : `On to ${WIZ_STEPS[step].name}`)}
              style={{ flexDirection: 'row-reverse' }}>
              {step === 3 && !simFresh ? 'Skip to Run' : `Next: ${WIZ_STEPS[step].name}`}
            </Btn>
          )}
          {step === 4 && !job && !shown && (
            <Btn kind="primary" icon={Play} onClick={run} busy={busy === 'run'} disabled={!canRun} tip={why}>Run allocation</Btn>
          )}
        </div>
      </ActionBar>
    </div>
  )
}
