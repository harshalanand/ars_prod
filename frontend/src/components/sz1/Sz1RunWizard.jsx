import { Fragment } from 'react'
import {
  Check, Lock, Upload, X, CheckCircle2, Scale, TrendingUp, ListOrdered, Repeat, ParkingSquare,
  ShieldCheck, GitMerge,
} from 'lucide-react'
import { C } from '@/theme/colors'
import { DataTable, OptionTiles, Chip, Btn, Tip, InfoTip, n } from './Sz1Ui'
import { ACTION_HELP } from './sz1Help'
import { WH, WH_HELP, SubHead } from './Sz1SessionBits'

// 1SZ ALC — Step 3 "Plan a run" wizard: the step rail and steps 1 (Stores)
// and 2 (Options). Steps 3 and 4 live in Sz1RunPreview / Sz1RunResult.
// Spec: public/docs/manual/sz1_alloc.md §D.

export const WIZ_STEPS = [
  { n: 1, key: 'stores', name: 'Stores', tip: 'Paste or upload store codes and check them. Only stores marked ok are allocated.' },
  { n: 2, key: 'options', name: 'Options', tip: 'Warehouse and store priority. Every run uses the live free stock left after the parked sessions.' },
  { n: 3, key: 'preview', name: 'Preview', tip: 'Compare Own RDC with All RDC on these stores. Writes nothing.' },
  { n: 4, key: 'run', name: 'Run', tip: 'Confirm and run. The result parks as a new session; approve it in Sessions.' },
]

export const MODES = [
  { key: 'weighted', name: 'Balanced', icon: Scale, tag: 'Total and size shortage, weighted', badge: 'Best',
    help: 'Inside each category + size: score = w × rank(MJ_SRT) + (1 − w) × rank(MJ_SZ_SRT), lowest first. A store short on both rises even when neither shortage is the largest.' },
  { key: 'mj_srt', name: 'Biggest total shortage', icon: TrendingUp, tag: 'Largest MJ_SRT first',
    help: 'Largest category shortage (MJ_SRT) first; the size shortage only separates exact ties.' },
  { key: 'sequence', name: 'My order', icon: ListOrdered, tag: 'Your store sequence',
    help: 'Paste the order: one store per line, optionally “HB05,1”. Stores you leave out go last.' },
]

export const MODE_NAME = Object.fromEntries(MODES.map(m => [m.key, m.name]))

/** One sentence that says what the run will do. */
export function optionSentence(f, stores) {
  const wh = f.cross_rdc === 'same' ? 'each store from its own RDC'
    : `the All RDC pool (${f.protect_other ? 'other RDC’s stores protected' : 'no protection'}, ${f.one_source ? 'one source per line' : 'lines split freely'})`
  const pr = f.mode === 'weighted' ? `Balanced priority (w ${(+f.weight_mj).toFixed(2)})` : f.mode === 'sequence' ? 'your store order' : 'biggest total shortage first'
  return `Allocate ${n(stores)} store${stores === 1 ? '' : 's'} from ${wh}, ${pr}, on the live free stock.`
}

/** "Uses live free stock after N parked session(s) (U units held)". parked = {count, units} or null while loading. */
export function liveStockText(parked) {
  if (!parked) return 'Uses the live free stock left after the parked sessions.'
  if (!parked.count) return 'Uses the live free stock. No session is parked, so nothing is held back.'
  return `Uses live free stock after ${n(parked.count)} parked session${parked.count === 1 ? '' : 's'} (${n(parked.units)} units held).`
}

export const LIVE_STOCK_HELP = {
  title: 'Live free stock',
  body: 'Every run starts from the warehouse free stock and each store’s size budget, less what the parked sessions hold. Approving a session moves its units into Pend ALC; discarding one gives them back.',
}

/** The one-line stock note of the Options step. */
export function LiveStockNote({ parked }) {
  return (
    <Tip tip={LIVE_STOCK_HELP} block>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '9px 12px', borderRadius: 10, cursor: 'help',
                    background: parked?.count ? C.amberBg : '#f8fafc', border: `1px solid ${parked?.count ? C.amberBd : C.cardBorder}`,
                    fontSize: 12.5, color: C.text }}>
        <ParkingSquare size={15} color={parked?.count ? C.amber : C.textMuted} style={{ flex: '0 0 15px' }} />
        <span style={{ minWidth: 0 }}>{liveStockText(parked)}</span>
      </div>
    </Tip>
  )
}

// ── step rail ──────────────────────────────────────────────────────────────
/** state(step) → 'done' | 'current' | 'open' | 'locked'; onGo(step). */
export function StepRail({ current, state, onGo }) {
  return (
    <nav aria-label="Plan a run" style={{ display: 'flex', alignItems: 'center', minWidth: 0 }}>
      {WIZ_STEPS.map((s, i) => {
        const st = s.n === current ? 'current' : state(s.n)
        const col = st === 'current' ? C.primary : st === 'done' ? C.green : st === 'open' ? C.textSub : '#cbd5e1'
        const filled = st === 'current' || st === 'done'
        return (
          <Fragment key={s.key}>
            <Tip tip={{ title: `${s.n} · ${s.name}`, body: st === 'locked' ? `${s.tip} Finish the steps before it first.` : s.tip }}>
              <button type="button" disabled={st === 'locked'} onClick={() => st !== 'current' && onGo(s.n)} aria-current={st === 'current' ? 'step' : undefined}
                style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 12px 5px 5px', borderRadius: 999, whiteSpace: 'nowrap',
                         cursor: st === 'locked' ? 'not-allowed' : st === 'current' ? 'default' : 'pointer',
                         border: `1.5px solid ${st === 'current' ? C.primary : 'transparent'}`, background: st === 'current' ? C.primaryLt : 'transparent' }}>
                <span style={{ width: 26, height: 26, borderRadius: '50%', display: 'grid', placeItems: 'center', flex: '0 0 26px', fontSize: 12, fontWeight: 800,
                               border: `2px solid ${col}`, background: filled ? col : '#fff', color: filled ? '#fff' : col }}>
                  {st === 'done' ? <Check size={13} strokeWidth={3} /> : st === 'locked' ? <Lock size={11} /> : s.n}
                </span>
                <span style={{ fontSize: 13, fontWeight: st === 'current' ? 800 : 600, color: st === 'current' ? C.primary : st === 'locked' ? C.textMuted : C.text }}>{s.name}</span>
              </button>
            </Tip>
            {i < WIZ_STEPS.length - 1 && (
              <span style={{ flex: '1 1 16px', minWidth: 12, maxWidth: 90, height: 2, borderRadius: 2, margin: '0 4px',
                             background: st === 'done' ? C.green : '#e2e8f0' }} />
            )}
          </Fragment>
        )
      })}
    </nav>
  )
}

// ── switch ─────────────────────────────────────────────────────────────────
export function Toggle({ on, onChange, label, help, icon: Icon, disabled, tone = WH.all }) {
  return (
    <Tip tip={{ title: label, body: disabled ? `${help} Applies to All RDC only.` : help }} block>
      <button type="button" role="switch" aria-checked={on} disabled={disabled} onClick={() => onChange(!on)}
        style={{ width: '100%', display: 'flex', alignItems: 'flex-start', gap: 10, textAlign: 'left', padding: '9px 11px', borderRadius: 10,
                 border: `1px solid ${on && !disabled ? tone.bd : C.cardBorder}`, background: disabled ? '#f8fafc' : '#fff',
                 cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.55 : 1 }}>
        <span style={{ position: 'relative', width: 34, height: 20, borderRadius: 999, flex: '0 0 34px', marginTop: 1, transition: 'background .15s',
                       background: on ? tone.fg : '#cbd5e1' }}>
          <span style={{ position: 'absolute', top: 2, left: on ? 16 : 2, width: 16, height: 16, borderRadius: '50%', background: '#fff',
                         boxShadow: '0 1px 3px rgba(15,23,42,.3)', transition: 'left .15s' }} />
        </span>
        <span style={{ minWidth: 0 }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5, fontWeight: 700, color: C.text }}>
            {Icon && <Icon size={13} color={on ? tone.fg : C.textMuted} />}{label}
            <span style={{ fontSize: 10.5, fontWeight: 800, color: on ? tone.fg : C.textMuted }}>{on ? 'ON' : 'OFF'}</span>
          </span>
          <span style={{ display: 'block', fontSize: 11.5, color: C.textSub, marginTop: 2, lineHeight: 1.4 }}>{help}</span>
        </span>
      </button>
    </Tip>
  )
}

// ── step 1: stores ─────────────────────────────────────────────────────────
export function StoresStep({ text, onText, file, onClearFile, onPickFile, onColumn, fileRef, checked, ok, skipped, okRdc, onCheck, busy, maxStores }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 18, alignItems: 'start' }}>
      <div style={{ minWidth: 0 }}>
        <SubHead right={<Tip tip="The most stores one run takes."><Chip>up to {n(maxStores || 2000)}</Chip></Tip>}>Store codes</SubHead>
        <Tip tip={file ? 'Codes come from the uploaded file. Remove the file to paste instead.' : 'Any separator works: spaces, commas, semicolons, pipes or new lines.'} block>
          <textarea className="input" rows={7} value={file ? '' : text} disabled={!!file}
            placeholder={file ? `Using ${file.name}` : 'Paste store codes, e.g.\nHB05, HB06, HY04'}
            onChange={e => onText(e.target.value)}
            style={{ width: '100%', fontFamily: 'ui-monospace, monospace', fontSize: 12.5, borderRadius: 10, resize: 'vertical' }} />
        </Tip>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
          <input ref={fileRef} type="file" accept=".xlsx,.xlsm,.xls,.csv,.txt,.tsv" style={{ display: 'none' }}
            onChange={e => { onPickFile(e.target.files?.[0]); e.target.value = '' }} />
          <Btn icon={Upload} onClick={() => fileRef.current?.click()} tip={ACTION_HELP.upload}>Upload a list</Btn>
          {file && (
            <>
              <Tip tip="The column that holds the store code">
                <select className="input" style={{ width: 140 }} value={file.used || ''} onChange={e => onColumn(e.target.value)}>
                  {file.columns.map(c => <option key={c} value={c}>{c}</option>)}
                </select>
              </Tip>
              <Tip tip={`${file.codes.length} codes read from column ${file.used}`}><Chip tone="blue">{file.name}</Chip></Tip>
              <Btn icon={X} onClick={onClearFile} tip="Remove the file and paste codes instead" />
            </>
          )}
          <Btn kind="primary" icon={CheckCircle2} style={{ marginLeft: 'auto' }} onClick={() => onCheck()} busy={busy}
            disabled={busy || !(file || text.trim())} tip={ACTION_HELP.check}>Check stores</Btn>
        </div>
      </div>

      <div style={{ minWidth: 0 }}>
        <SubHead right={checked && (
          <span style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap' }}>
            <Tip tip="Stores that will be allocated"><Chip tone={ok.length ? 'green' : 'red'}>{n(ok.length)} ok</Chip></Tip>
            {skipped.length > 0 && <Tip tip="Codes not found, not listed or with nothing short. They are left out."><Chip tone="amber">{n(skipped.length)} skipped</Chip></Tip>}
            {Object.entries(okRdc).map(([r, c]) => <Tip key={r} tip={`Ok stores whose home warehouse is ${r}`}><Chip tone="indigo">{r} · {c}</Chip></Tip>)}
          </span>
        )}>Check result</SubHead>
        {checked ? (
          <DataTable rows={checked} exportName="1SZ_ALC_store_check.csv" maxHeight={260}
            cols={[['ST_CD', 'Store'], ['ST_NM', 'Name'], ['RDC', 'RDC'], ['STATUS', 'Status'], ['SHORT_ROWS', 'Short sizes'], ['SHORT_QTY', 'Short qty']]} />
        ) : (
          <div style={{ border: `1.5px dashed ${C.cardBorder}`, borderRadius: 12, padding: '26px 18px', textAlign: 'center', color: C.textMuted, fontSize: 12.5, lineHeight: 1.6 }}>
            Paste or upload codes, then press <b style={{ color: C.text }}>Check stores</b>.<br />
            Each code is looked up: in the store master, listed, and short of at least one size.
          </div>
        )}
      </div>
    </div>
  )
}

// ── step 2: options ────────────────────────────────────────────────────────
function WarehouseTile({ k, name, badge, on, onPick }) {
  const t = WH[k]
  const Icon = t.icon
  return (
    <Tip tip={{ title: name, body: WH_HELP[k] }} block>
      <button type="button" aria-pressed={on} onClick={onPick}
        style={{ width: '100%', height: '100%', textAlign: 'left', cursor: 'pointer', borderRadius: 14, padding: '14px 14px 13px',
                 background: on ? t.bg : C.card, border: `2px solid ${on ? t.fg : C.cardBorder}`, transition: 'all .15s',
                 boxShadow: on ? `0 6px 18px ${t.fg}22` : 'none' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ width: 34, height: 34, borderRadius: 10, display: 'grid', placeItems: 'center', flex: '0 0 34px',
                         background: on ? t.fg : '#f1f5f9', color: on ? '#fff' : C.textSub }}><Icon size={17} /></span>
          <span style={{ fontSize: 15, fontWeight: 800, color: on ? t.fg : C.text }}>{name}</span>
          {badge && <span style={{ marginLeft: 'auto', fontSize: 10, fontWeight: 800, color: C.green, background: C.greenBg, borderRadius: 999, padding: '2px 8px' }}>{badge}</span>}
          {on && !badge && <Check size={16} color={t.fg} style={{ marginLeft: 'auto' }} />}
        </div>
        <div style={{ fontSize: 12, color: C.textSub, marginTop: 8, lineHeight: 1.5 }}>{WH_HELP[k]}</div>
      </button>
    </Tip>
  )
}

export function OptionsStep({ form, set, okCount, parked }) {
  const all = form.cross_rdc === 'all'
  const w = +form.weight_mj
  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 20, alignItems: 'start' }}>
        <div style={{ minWidth: 0 }}>
          <SubHead right={<InfoTip tip="Where a store may draw stock from. Preview compares both on your stores." />}>Warehouse</SubHead>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10 }}>
            <WarehouseTile k="same" name="Own RDC" on={!all} onPick={() => set('cross_rdc', 'same')} />
            <WarehouseTile k="all" name="All RDC" badge="Default" on={all} onPick={() => set('cross_rdc', 'all')} />
          </div>
          <div style={{ marginTop: 10, padding: 10, borderRadius: 12, background: all ? WH.all.bg : '#f8fafc', border: `1px solid ${all ? WH.all.bd : C.cardBorder}`,
                        display: 'grid', gap: 8 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: all ? WH.all.fg : C.textMuted }}>All RDC switches{all ? '' : ' · pick All RDC to use them'}</div>
            <Toggle on={form.protect_other} onChange={v => set('protect_other', v)} disabled={!all} icon={ShieldCheck}
              label="Protect other RDC’s stores" help={WH_HELP.protect} />
            <Toggle on={form.one_source} onChange={v => set('one_source', v)} disabled={!all} icon={GitMerge}
              label="One source per line" help={WH_HELP.one} />
          </div>
        </div>

        <div style={{ minWidth: 0, display: 'grid', gap: 16 }}>
          <div>
            <SubHead right={<InfoTip tip="Which store is served first inside each category + size. Hover a tile for the rule." />}>Store priority</SubHead>
            <OptionTiles value={form.mode} onChange={v => set('mode', v)} options={MODES} />
            {form.mode === 'weighted' && (
              <div style={{ marginTop: 10, padding: '10px 12px', background: '#f8fafc', borderRadius: 10, border: `1px solid ${C.cardBorder}` }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5, color: C.textSub, fontWeight: 600 }}>
                  <span>Size shortage MJ_SZ_SRT</span>
                  <span style={{ color: C.text, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>w = {w.toFixed(2)}</span>
                  <span>Total shortage MJ_SRT</span>
                </div>
                <Tip tip={`${Math.round(w * 100)}% on the total-shortage rank, ${100 - Math.round(w * 100)}% on the size-shortage rank`} block>
                  <input type="range" min={0} max={1} step={0.05} value={form.weight_mj} onChange={e => set('weight_mj', +e.target.value)}
                    aria-label="Weight on the total-shortage rank" style={{ width: '100%', accentColor: C.primary }} />
                </Tip>
              </div>
            )}
            {form.mode === 'sequence' && (
              <Tip tip="One store per line, optionally with its position: HB05,1. Stores you leave out go last." block>
                <textarea className="input" rows={4} value={form.sequence} placeholder={'HB09,1\nHB05,2\nHB13,3'}
                  onChange={e => set('sequence', e.target.value)}
                  style={{ width: '100%', marginTop: 10, fontFamily: 'ui-monospace, monospace', fontSize: 12.5, borderRadius: 10,
                           borderColor: form.sequence.trim() ? undefined : C.amberBd }} />
              </Tip>
            )}
          </div>
          <div>
            <SubHead right={<InfoTip tip={LIVE_STOCK_HELP} />}>Stock</SubHead>
            <LiveStockNote parked={parked} />
          </div>
        </div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '10px 12px', borderRadius: 10, background: '#f8fafc',
                    border: `1px solid ${C.cardBorder}`, fontSize: 12.5, color: C.text }}>
        <CheckCircle2 size={15} color={C.green} />
        <span style={{ fontWeight: 600, flex: '1 1 300px', minWidth: 0 }}>{optionSentence(form, okCount)}</span>
        <Tip tip={{ title: 'Refill rounds (I_ROD)', body: 'I_ROD from ARS_CALC_ST_ART, else ARS_CALC_ST_MAJ_CAT, else 1; A / A_MIX at least 2; at most 12. Round r target = packround(r × ART_MBQ − store stock). The size and category budgets stay at one round, so extra rounds change which articles go, never how much a store may receive. Everyone gets round 1 before round 2.' }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11.5, color: C.textSub, cursor: 'help' }}>
            <Repeat size={13} color={C.indigo} /> Refill rounds are fixed
          </span>
        </Tip>
      </div>
      {form.mode === 'sequence' && !form.sequence.trim() && (
        <div style={{ fontSize: 12, color: C.amber, fontWeight: 600 }}>My order: paste a store order to go on.</div>
      )}
    </div>
  )
}
