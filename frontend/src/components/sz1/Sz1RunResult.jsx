import { X, CheckCircle2, Pencil, Store, Network, GitBranch, Truck, Boxes, Target, ExternalLink, RotateCcw, Cpu, Info, ParkingSquare } from 'lucide-react'
import { C } from '@/theme/colors'
import { Banner, Kpi, Checks, Btn, Chip, Tip, n, when, pct } from './Sz1Ui'
import { WarehouseChips, whOptions, SubHead, StatusBadge, statusOf } from './Sz1SessionBits'
import { MODE_NAME, liveStockText, LIVE_STOCK_HELP } from './Sz1RunWizard'

// 1SZ ALC — wizard step 4: confirm, run (progress polling lives in the page),
// result with the limit checks. Spec: public/docs/manual/sz1_alloc.md §D–E.

function Row({ k, children, onEdit, editTip }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '150px minmax(0, 1fr) auto', gap: 12, alignItems: 'center', padding: '10px 0',
                  borderTop: `1px solid #f1f5f9` }}>
      <span style={{ fontSize: 12, fontWeight: 700, color: C.textSub }}>{k}</span>
      <span style={{ fontSize: 13, color: C.text, minWidth: 0, display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>{children}</span>
      {onEdit ? <Btn icon={Pencil} onClick={onEdit} tip={editTip}>Change</Btn> : <span />}
    </div>
  )
}

function Confirm({ form, setNote, okCount, okRdc, skippedCount, gates, simChosen, onEdit, parked }) {
  const t = simChosen?.totals
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 20, alignItems: 'start' }}>
      <div style={{ minWidth: 0 }}>
        <SubHead>This run</SubHead>
        <div style={{ borderBottom: `1px solid #f1f5f9` }}>
          <Row k="Stores" onEdit={() => onEdit(1)} editTip="Back to step 1">
            <b style={{ fontVariantNumeric: 'tabular-nums' }}>{n(okCount)}</b> ok
            {Object.entries(okRdc).map(([r, c]) => <Chip key={r} tone="indigo">{r} · {c}</Chip>)}
            {skippedCount > 0 && <Tip tip="Left out: not found, not listed or nothing short"><Chip tone="amber">{n(skippedCount)} skipped</Chip></Tip>}
          </Row>
          <Row k="Warehouse" onEdit={() => onEdit(2)} editTip="Back to step 2">
            <WarehouseChips cross={form.cross_rdc} protect={form.protect_other} one={form.one_source} />
          </Row>
          <Row k="Store priority" onEdit={() => onEdit(2)} editTip="Back to step 2">
            {MODE_NAME[form.mode]}{form.mode === 'weighted' ? <span style={{ color: C.textSub }}>w {(+form.weight_mj).toFixed(2)}</span> : null}
          </Row>
          <Row k="Stock">
            <Tip tip={LIVE_STOCK_HELP}><span style={{ cursor: 'help' }}>{liveStockText(parked)}</span></Tip>
          </Row>
          <Row k="Preview said" onEdit={() => onEdit(3)} editTip="Back to step 3">
            {t ? (
              <>
                <b style={{ fontVariantNumeric: 'tabular-nums' }}>{n(t.ALLOC_QTY)}</b> pcs ·
                <span>{n(t.STORES_SERVED)} stores served</span> ·
                <span>{t.FILL_PCT != null ? `${(+t.FILL_PCT).toFixed(1)}%` : '—'} of the size need</span>
              </>
            ) : <span style={{ color: C.textMuted }}>Not previewed with these choices</span>}
          </Row>
        </div>
      </div>
      <div style={{ minWidth: 0, display: 'grid', gap: 12 }}>
        <div>
          <SubHead>Note</SubHead>
          <Tip tip="Kept on the session header, e.g. the wave or the reason for the run." block>
            <input className="input" placeholder="Optional, e.g. Wave 2 after the GRT" value={form.note} maxLength={200}
              onChange={e => setNote(e.target.value)} style={{ width: '100%' }} />
          </Tip>
        </div>
        {gates && !gates.ok ? (
          <Banner tone="red" label="Cannot run" text={(gates.block || []).join(' · ') || 'See Readiness at the top of the page.'} />
        ) : (
          <div style={{ display: 'flex', gap: 8, padding: '10px 12px', borderRadius: 10, background: '#f8fafc', border: `1px solid ${C.cardBorder}`,
                        fontSize: 12, color: C.textSub, lineHeight: 1.55 }}>
            <Info size={15} color={C.primary} style={{ flex: '0 0 15px', marginTop: 1 }} />
            <span>
              <b style={{ color: C.text }}>Run allocation</b> parks a new session that holds its stock inside 1SZ. The lines and the limit checks commit
              together: a breach or a cancel writes nothing. Approve the session into Pend ALC on the Sessions page.
            </span>
          </div>
        )}
      </div>
    </div>
  )
}

function Running({ job, onCancel }) {
  const p = job.progress || {}
  return (
    <div style={{ border: `1px solid ${C.indigoBd}`, background: C.indigoBg, borderRadius: 12, padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, marginBottom: 10 }}>
        <Cpu size={16} color={C.primary} />
        <b style={{ color: C.text }}>Session {job.SESSION_ID}</b>
        <span style={{ color: C.textSub }}>{p.text || 'Starting'}</span>
        <span style={{ marginLeft: 'auto', color: C.primary, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>{job.PROGRESS_PCT || 0}%</span>
      </div>
      <div style={{ height: 10, borderRadius: 999, background: '#fff', overflow: 'hidden', border: `1px solid ${C.indigoBd}` }}>
        <div style={{ width: `${job.PROGRESS_PCT || 0}%`, height: '100%', borderRadius: 999, transition: 'width .4s',
                      background: 'linear-gradient(90deg, #4f46e5, #7c3aed)' }} />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, color: C.textSub, fontVariantNumeric: 'tabular-nums' }}>
          {p.lines ? `${n(p.lines)} candidate lines · ` : ''}{p.units != null ? `${n(p.units)} units so far · ` : ''}lines and limit checks commit together, then the session parks.
        </span>
        <Btn icon={X} onClick={onCancel} style={{ marginLeft: 'auto' }} tip="Stop the run. Nothing is written.">Cancel</Btn>
      </div>
    </div>
  )
}

function Result({ s, simChosen, onOpen, onNew }) {
  const wh = whOptions(s)
  const parked = statusOf(s) === 'PARKED'
  const head = (
    <div style={{ display: 'grid', gap: 10, marginBottom: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <CheckCircle2 size={17} color={parked ? C.green : C.red} />
        <b style={{ fontSize: 14, color: C.text }}>Session {s.SESSION_ID}</b>
        <StatusBadge s={s} />
        <WarehouseChips {...wh} />
        <Chip>{MODE_NAME[s.PRIORITY_MODE] || s.PRIORITY_MODE}{s.WEIGHT_MJ != null ? ` ${s.WEIGHT_MJ}` : ''}</Chip>
        <span style={{ fontSize: 11.5, color: C.textMuted }}>{when(s.SESSION_AT)} · {s.CREATED_BY} · {Math.round(s.DURATION_SEC || 0)} s</span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Btn icon={RotateCcw} onClick={onNew} tip="Start a new plan. Your stores and options are kept.">Plan another run</Btn>
          {!parked && <Btn icon={ExternalLink} onClick={() => onOpen(s.SESSION_ID)} tip="Open this session in Sessions">Open in Sessions</Btn>}
        </span>
      </div>
      {parked && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '10px 12px', borderRadius: 10,
                      background: C.amberBg, border: `1px solid ${C.amberBd}` }}>
          <ParkingSquare size={16} color={C.amber} />
          <span style={{ fontSize: 13, color: C.text, flex: '1 1 280px', minWidth: 0 }}>
            <b>Parked as session {s.SESSION_ID}</b> — approve it in Sessions. It holds its {n(s.ALLOC_QTY ?? s.summary?.totals?.ALLOC_QTY)} units
            inside 1SZ until you approve it into Pend ALC or discard it.
          </span>
          <Btn kind="primary" icon={ExternalLink} onClick={() => onOpen(s.SESSION_ID)}
            tip="Open the session: re-check, approve into Pend ALC, remove stores, re-fit or discard">Open to approve</Btn>
        </div>
      )}
    </div>
  )
  if (!parked) {
    return (
      <div style={{ display: 'grid', gap: 12 }}>
        {head}
        <Banner tone={s.STATUS === 'CANCELLED' ? 'amber' : 'red'} label={s.STATUS} text={`${s.progress?.text || ''} ${String(s.ERROR || '').split('\n')[0]}`} />
        {(s.checks || []).length > 0 && <div><SubHead>Allocation limits</SubHead><Checks items={s.checks} /></div>}
      </div>
    )
  }
  const t = s.summary?.totals || {}
  const said = simChosen?.totals?.ALLOC_QTY
  return (
    <div>
      {head}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(165px, 1fr))', gap: 10 }}>
        <Kpi k="Allocated" v={n(t.ALLOC_QTY ?? s.ALLOC_QTY)} x={said != null ? `preview said ${n(said)}` : `${pct(t.ALLOC_QTY, t.SZ_SRT)} of the size need`}
          tone="green" icon={Truck} tip="Units given, in whole packs." />
        <Kpi k="Size need met" v={pct(t.ALLOC_QTY, t.SZ_SRT)} x={`of ${n(t.SZ_SRT)} short`} icon={Target} tip="Units allocated ÷ the size shortage of the run’s stores." />
        <Kpi k="Packs" v={n(t.PACKS ?? s.PACKS)} x={`${n(t.LINES_SERVED)} lines`} icon={Boxes} tone="indigo" />
        <Kpi k="Stores served" v={`${n(t.STORES_SERVED)} / ${n(t.STORES)}`} icon={Store} tone="blue" tip="Stores that got at least one pack / stores in the run." />
        <Kpi k="From other RDC" v={n(t.CROSS_QTY)} x={pct(t.CROSS_QTY, t.ALLOC_QTY)} icon={Network} tone="amber" tip="Units drawn from a warehouse other than the store’s own." />
        <Kpi k="Split lines" v={n(t.SPLIT_LINES)} x="served from 2 RDCs" icon={GitBranch} tip="Store × article lines whose packs came from both warehouses." />
      </div>
      <div style={{ marginTop: 16 }}><SubHead>Allocation limits</SubHead><Checks items={s.checks || []} /></div>
    </div>
  )
}

/** Step 4: a running job, else the result just shown, else the confirmation. */
export function RunStep({ job, shown, onCancel, onOpen, onNew, simChosen, ...confirm }) {   // confirm includes parked = {count, units}
  if (job) return <Running job={job} onCancel={onCancel} />
  if (shown) return <Result s={shown} simChosen={simChosen} onOpen={onOpen} onNew={onNew} />
  return <Confirm simChosen={simChosen} {...confirm} />
}
