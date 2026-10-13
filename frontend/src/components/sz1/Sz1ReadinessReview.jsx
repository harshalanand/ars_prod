import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, X, Loader2, ArrowLeft, List, Download, RefreshCw, ShieldCheck, Lightbulb, ChevronRight } from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API, saveBlob } from '@/services/api'
import { C } from '@/theme/colors'
import { Modal, DataTable, Tip, InfoTip, Chip, Btn, when, errText } from './Sz1Ui'

// 1SZ ALC — Readiness review: every check of a scope in one popup, filtered by
// Blocks / Warnings / Info / Passed, and — for an item with rows behind it
// (item.has_rows) — the list itself: GET /sz1/readiness/{scope}/items/{key}/rows,
// full CSV from …/export. Read only. Spec: sz1_alloc.md §A.

export const GROUPS = [
  { key: 'sources', name: 'Sources', help: 'The tables this step reads are there and hold rows.' },
  { key: 'order', name: 'Order', help: 'The builds were done in the right order, and no other 1SZ ALC job is running.' },
  { key: 'freshness', name: 'Freshness', help: 'A source table changed after the build, so the build may be out of date.' },
  { key: 'coverage', name: 'Coverage', help: 'Stores or categories that will get no budget.' },
  { key: 'sessions', name: 'Sessions', help: 'Earlier runs still holding stock — a new run starts from what they left.' },
  { key: 'rules', name: 'Rules', help: 'Settings changed since the build; generate again to apply them.' },
]
export const LEVEL = {
  ok: { fg: C.green, bg: C.greenBg, bd: C.greenBd, icon: <Check size={11} strokeWidth={3} />, word: 'Passed' },
  info: { fg: C.blue, bg: C.blueBg, bd: C.blueBd, icon: <span style={{ fontSize: 11, fontWeight: 900, lineHeight: 1, fontStyle: 'italic' }}>i</span>, word: 'Info' },
  warn: { fg: C.amber, bg: C.amberBg, bd: C.amberBd, icon: <span style={{ fontSize: 11, fontWeight: 900, lineHeight: 1 }}>!</span>, word: 'Warning' },
  block: { fg: C.red, bg: C.redBg, bd: C.redBd, icon: <X size={11} strokeWidth={3} />, word: 'Blocks' },
}

export function Dot({ level }) {
  const l = LEVEL[level] || LEVEL.ok
  return (
    <Tip tip={l.word}>
      <span style={{ width: 18, height: 18, borderRadius: '50%', display: 'grid', placeItems: 'center', flex: '0 0 18px',
                     background: l.fg, color: '#fff', marginTop: 1 }}>{l.icon}</span>
    </Tip>
  )
}

const CATS = [
  { key: 'block', label: 'Blocks', tip: 'Refuse the build or run until fixed.' },
  { key: 'warn', label: 'Warnings', tip: 'Let it go ahead; saved with the build or session.' },
  { key: 'info', label: 'Info', tip: 'Passed, but worth a look: checks that were skipped, and figures such as parked sessions or the zero-CONT gap.' },
  { key: 'ok', label: 'Passed', tip: 'Checks with nothing to report.' },
]
const INFO_KEYS = /^(cov_gap|sessions_held|sessions_approved|balance_drift)$/

/** block | warn | info | ok — "info" = passed but skipped, or a figure worth reading. */
export function catOf(it) {
  if (it.level === 'block' || it.level === 'warn') return it.level
  if (/^Skipped/.test(it.detail || '')) return 'info'
  if (INFO_KEYS.test(it.key) && it.value && !/^(none|0\b)/i.test(String(it.value))) return 'info'
  return 'ok'
}

const SCOPE_WORDS = { build1: 'Step 1 build (Supply)', build2: 'Step 2 build (Demand)', run: 'Step 3 run' }
const TO = {
  supply: { label: 'Prepare → Supply', to: '/sz1-alloc/prepare?tab=supply' },
  demand: { label: 'Prepare → Demand', to: '/sz1-alloc/prepare?tab=demand' },
  gap: { label: 'Prepare → Gap (Master data quality)', to: '/sz1-alloc/prepare?tab=gap' },
  sessions: { label: 'Sessions', to: '/sz1-alloc/sessions' },
  settings: { label: 'Settings', to: '/sz1-alloc/settings' },
  grid: { label: 'Grid Builder', to: '/data-prep/store-stock' },
}

/** What to do about an item: { text, links: [{label, to}] } or null. */
function hintFor(it, scope) {
  const k = it.key || ''
  if (k.startsWith('src_')) {
    const t = k.slice(4)
    return { text: /CALC_ST/.test(t)
      ? 'Optional: without it I_ROD falls back to 1. Load it with the ARS calculation before the run if I_ROD matters.'
      : 'The step cannot read a missing or empty table. Load it (its own job or upload) first, then Check again.',
             links: /GRID_MJ/.test(t) ? [TO.grid] : [] }
  }
  if (k === 'job') return { text: 'Only one 1SZ ALC job runs at a time. Wait for it to finish, then Check again.', links: [] }
  if (k === 'step1_built') return { text: 'Generate Supply (Step 1) first — Demand and the run read its candidates.', links: [TO.supply] }
  if (k === 'step2_built' || k === 'step2_after_step1') {
    return { text: 'Generate Demand (Step 2) after the latest Supply build, so its MAJ_CAT + size pairs match the candidates.', links: [TO.demand] }
  }
  if (k.startsWith('fresh_')) {
    return { text: scope === 'build2'
      ? 'A source changed after the Supply build. Generate Supply (Step 1) again before Demand.'
      : 'A source changed after the build that read it. Rebuild Supply / Demand: Step 1 sources → Supply, then Demand; Step 2-only sources → Demand.',
             links: scope === 'build2' ? [TO.supply] : [TO.supply, TO.demand] }
  }
  if (k === 'rules') return { text: 'The Step 1 rule settings changed after the Supply build. Generate Supply again, then Demand, to apply them.', links: [TO.settings, TO.supply] }
  if (k === 'cov_grid') {
    return { text: 'These stores have no grid row for the category, so their budget is 0 and 1SZ sends them nothing in it. Fix: grid rows at company level (Grid Builder rule) and display space for the category — or accept the 0 budget.'
      + (scope === 'run' ? ' Where GRID_NOW = Y the grid has the row today: rebuilding Demand picks it up.' : ''),
             links: [TO.grid, ...(scope === 'run' ? [TO.demand] : []), TO.gap] }
  }
  if (k === 'cov_curve') return { text: 'No size curve: every size gets 0. Upload a CO size curve for the category in Master_CONT_SZ (it covers every store without its own), or the store\'s curve; then rebuild Demand.', links: [TO.gap, TO.demand] }
  if (k === 'cov_gap') return { text: 'Information. These articles have no CONT % anywhere in Master_CONT_SZ, so Supply keeps them out (EXCLUDED = Y). Add the size to the CO curve, then rebuild Supply.', links: [TO.gap] }
  if (k === 'sessions_held' || k.startsWith('sessions_stale')) {
    const id = k.startsWith('sessions_stale_') ? k.slice('sessions_stale_'.length) : null
    return { text: 'Approve or discard in Sessions. Parked stock is held inside 1SZ only — Listing can\'t see it until it is approved into Pend ALC.',
             links: [id ? { label: `Open session ${id}`, to: `/sz1-alloc/sessions?id=${encodeURIComponent(id)}` } : TO.sessions] }
  }
  if (k === 'sessions_approved') return { text: 'Nothing to do: a run nets approved sessions as live open pending.', links: [TO.sessions] }
  if (k === 'balance_drift') return { text: 'Nothing to do before a run — it re-applies the balances first. To update the stored columns now, use Re-apply balances on Prepare.', links: [TO.supply] }
  return null
}

const linkBtn = { display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11.5, fontWeight: 600, color: C.primary, background: 'none',
                  border: 'none', cursor: 'pointer', padding: 0, whiteSpace: 'nowrap' }
const smallBtn = { display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11.5, fontWeight: 600, color: C.primary, background: C.primaryLt,
                   border: `1px solid ${C.indigoBd}`, borderRadius: 7, padding: '2px 8px', cursor: 'pointer', whiteSpace: 'nowrap' }

function Hint({ hint, go }) {
  if (!hint) return null
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', background: '#f8fafc', border: `1px solid ${C.cardBorder}`, borderRadius: 10,
                  padding: '8px 10px', fontSize: 12, color: C.textSub, lineHeight: 1.45 }}>
      <Lightbulb size={14} color={C.amber} style={{ flex: '0 0 14px', marginTop: 2 }} />
      <div style={{ minWidth: 0 }}>
        <b style={{ color: C.text }}>What to do · </b>{hint.text}
        {hint.links.length > 0 && (
          <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 4 }}>
            {hint.links.map(l => (
              <button key={l.to} type="button" style={linkBtn} onClick={() => go(l.to)}>{l.label} <ChevronRight size={12} /></button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * open / onClose; scope; res — the readiness result already loaded by the panel;
 * loading; onRecheck(); action(item) — the panel's optional block / warn action;
 * startItem — open straight on this item's list.
 */
export default function Sz1ReadinessReview({ open, onClose, scope, res, loading, onRecheck, action, startItem }) {
  const nav = useNavigate()
  const [show, setShow] = useState(['block', 'warn'])
  const [view, setView] = useState(null)              // the item whose list is open
  const [list, setList] = useState(null)              // { data } | { err } while view is set
  const [exporting, setExporting] = useState(false)
  const seq = useRef(0)

  const start = useRef(startItem)
  start.current = startItem
  useEffect(() => { if (open) { setShow(['block', 'warn']); setView(start.current || null) } }, [open])
  useEffect(() => {
    if (!view) { setList(null); return }
    const my = ++seq.current
    setList(null)
    sz1API.readinessRows(scope, view.key)
      .then(({ data: r }) => { if (my === seq.current) setList({ data: r?.data || {} }) })
      .catch(e => { if (my === seq.current) setList({ err: errText(e, 'no answer') }) })
  }, [view, scope])

  const go = to => { onClose(); nav(to) }
  const items = res?.items || []
  const counts = Object.fromEntries(CATS.map(c => [c.key, items.filter(i => catOf(i) === c.key).length]))
  const shown = items.filter(i => show.includes(catOf(i)))
  const known = GROUPS.map(g => g.key)
  const groups = [...GROUPS, ...[...new Set(items.map(i => i.group))].filter(g => g && !known.includes(g)).map(g => ({ key: g, name: g, help: null }))]
    .map(g => ({ ...g, items: shown.filter(i => i.group === g.key) }))
    .filter(g => g.items.length)
  const toggle = k => setShow(s => (s.includes(k) ? s.filter(x => x !== k) : [...s, k]))

  const doExport = async () => {
    setExporting(true)
    try { const r = await sz1API.exportReadiness(scope, view.key); toast.success(`Saved ${saveBlob(r, `1SZ_READINESS_${scope}_${view.key}.csv`)}`) }
    catch (e) { toast.error(errText(e, 'Export failed')) }
    finally { setExporting(false) }
  }

  const footer = (
    <>
      <span style={{ fontSize: 11.5, color: C.textMuted }}>{res?.checked_at ? `checked ${when(res.checked_at)}` : 'not checked yet'}</span>
      <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
        <Btn icon={RefreshCw} onClick={onRecheck} busy={loading} disabled={loading} tip="Run every check again (read only)">Check again</Btn>
        <Btn onClick={onClose}>Close</Btn>
      </div>
    </>
  )

  return (
    <Modal open={open} onClose={onClose} icon={ShieldCheck} width={1120} footer={footer}
      title={`Readiness review — ${SCOPE_WORDS[scope] || scope}`}>
      {!res && <div style={{ fontSize: 12, color: C.textMuted }}>{loading ? 'Checking…' : 'Readiness could not be checked — Check again.'}</div>}

      {res && !view && (
        <div style={{ display: 'grid', gap: 12 }}>
          {/* summary: filter chips */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            {CATS.map(c => {
              const on = show.includes(c.key)
              const l = LEVEL[c.key]
              return (
                <Tip key={c.key} tip={{ title: c.label, body: `${c.tip} Click to ${on ? 'hide' : 'show'} them.` }}>
                  <button type="button" onClick={() => toggle(c.key)} aria-pressed={on}
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 7, padding: '5px 12px', borderRadius: 999, cursor: 'pointer',
                             border: `1.5px solid ${on ? l.fg : C.cardBorder}`, background: on ? l.bg : C.card,
                             color: on ? l.fg : C.textSub, fontSize: 12, fontWeight: 700 }}>
                    <span style={{ fontSize: 15, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>{counts[c.key]}</span> {c.label}
                  </button>
                </Tip>
              )
            })}
            <span style={{ fontSize: 11.5, color: C.textMuted, marginLeft: 'auto' }}>
              {shown.length} of {items.length} checks shown
            </span>
          </div>

          {!groups.length && (
            <div style={{ fontSize: 12.5, color: C.textSub, padding: '10px 2px' }}>
              {!counts.block && !counts.warn && show.every(k => k === 'block' || k === 'warn')
                ? <>Nothing blocks and nothing warns — the {SCOPE_WORDS[scope] || 'step'} is ready. Click <b>Info</b> or <b>Passed</b> to see the other checks.</>
                : 'No check under the chosen filters.'}
            </div>
          )}

          {groups.map(g => (
            <div key={g.key} style={{ minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, fontWeight: 800, color: C.textSub,
                            textTransform: 'uppercase', letterSpacing: '.05em', marginBottom: 6 }}>
                {g.name}{g.help && <InfoTip tip={{ title: g.name, body: g.help }} size={12} />}
                <span style={{ flex: 1, height: 1, background: C.cardBorder, marginLeft: 4 }} />
              </div>
              <div style={{ display: 'grid', gap: 6 }}>
                {g.items.map(it => {
                  const cat = catOf(it)
                  const hint = cat !== 'ok' ? hintFor(it, scope) : null
                  const act = it.level !== 'ok' && action ? action(it) : null
                  return (
                    <div key={it.key} style={{ display: 'flex', gap: 9, alignItems: 'flex-start', fontSize: 12, padding: '7px 10px', borderRadius: 10,
                                               border: `1px solid ${cat === 'ok' ? C.cardBorder : LEVEL[cat].bd}`, minWidth: 0 }}>
                      <Dot level={cat} />
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', flexWrap: 'wrap' }}>
                          <span style={{ fontWeight: 700, color: C.text }}>{it.name}</span>
                          <span style={{ fontSize: 11, fontWeight: 700, color: LEVEL[cat].fg }}>{LEVEL[cat].word}</span>
                          {hint && <InfoTip tip={{ title: 'What to do', body: hint.text }} size={12} />}
                          {it.value != null && it.value !== '' && (
                            <span style={{ marginLeft: 'auto', color: C.textSub, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>{it.value}</span>
                          )}
                        </div>
                        {it.detail && <div style={{ color: cat === 'ok' ? C.textMuted : C.textSub, lineHeight: 1.45, marginTop: 2, overflowWrap: 'anywhere' }}>{it.detail}</div>}
                        {(it.has_rows || act) && (
                          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 6 }}>
                            {it.has_rows && (
                              <Tip tip="Open the rows behind this check — sort, filter and export them">
                                <button type="button" style={smallBtn} onClick={() => setView(it)}><List size={12} /> View list</button>
                              </Tip>
                            )}
                            {act && <span onClickCapture={onClose}>{act}</span>}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      {res && view && (() => {
        const cat = catOf(view)
        const d = list?.data
        return (
          <div style={{ display: 'grid', gap: 10, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <button type="button" style={linkBtn} onClick={() => setView(null)}><ArrowLeft size={13} /> All checks</button>
              <span style={{ width: 1, height: 16, background: C.cardBorder }} />
              <Dot level={cat} />
              <span style={{ fontSize: 13.5, fontWeight: 800, color: C.text }}>{view.name}</span>
              <Chip tone={{ block: 'red', warn: 'amber', info: 'blue', ok: 'green' }[cat]}>{LEVEL[cat].word}</Chip>
              {view.value && <span style={{ fontSize: 12, color: C.textSub }}>{view.value}</span>}
              <div style={{ marginLeft: 'auto' }}>
                <Btn icon={Download} onClick={doExport} busy={exporting} disabled={exporting || !d || !d.total}
                  tip="Every row of this list as CSV, straight from the server (not only the rows on screen). Read only.">Export full list</Btn>
              </div>
            </div>
            {view.detail && <div style={{ fontSize: 12, color: C.textSub, lineHeight: 1.45 }}>{view.detail}</div>}
            <Hint hint={hintFor(view, scope)} go={go} />
            {!list && <div style={{ fontSize: 12, color: C.textMuted, display: 'flex', alignItems: 'center', gap: 6 }}><Loader2 size={13} className="animate-spin" /> Reading the list…</div>}
            {list?.err && <div style={{ fontSize: 12, color: C.red }}>Could not read the list: {list.err}</div>}
            {d && (
              <>
                <div style={{ fontSize: 11.5, color: C.textMuted, lineHeight: 1.45 }}>
                  {d.note}{d.seconds != null && ` · read in ${d.seconds} s`}
                  {d.truncated && <b style={{ color: C.amber }}> · showing the first {d.rows.length.toLocaleString()} of {d.total.toLocaleString()} rows — Export full list for all.</b>}
                </div>
                <DataTable rows={d.rows || []} cols={d.columns?.length ? d.columns : undefined} maxHeight="min(52vh, 480px)"
                  empty="No rows — nothing to review here." exportName={`1SZ_readiness_${scope}_${view.key}.csv`} />
              </>
            )}
          </div>
        )
      })()}
    </Modal>
  )
}
