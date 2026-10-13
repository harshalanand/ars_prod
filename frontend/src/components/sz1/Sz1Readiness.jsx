import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, ChevronDown, ChevronRight, RefreshCw, ShieldCheck, ListChecks, List } from 'lucide-react'
import { sz1API } from '@/services/api'
import { C } from '@/theme/colors'
import { Tip, InfoTip, Chip, when, errText } from './Sz1Ui'
import Sz1ReadinessReview, { GROUPS, Dot } from './Sz1ReadinessReview'

// 1SZ ALC — "validate first, then create". Shows GET /sz1/readiness?scope=
// (build1 | build2 | run) before anything is written. Spec: sz1_alloc.md §A.
// Opens by itself only when something blocks (warnings show as a one-line
// summary); passed checks fold into a count unless "Show passed checks" is on.
// The title, the summary chip and "Review" open the review popup
// (Sz1ReadinessReview): every check filtered by level, and the rows behind it.

const FOLD_EVENT = 'sz1-panels'          // the page header's Collapse all / Expand all (Sz1Ui)

/**
 * scope — 'build1' | 'build2' | 'run'; refreshKey — bump to check again;
 * onLoaded(result | null) — null when the check could not run;
 * action(item) — optional node shown next to a block / warn item (e.g. "Build it").
 */
export default function Sz1Readiness({ scope, refreshKey = 0, onLoaded, action }) {
  const [res, setRes] = useState(null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [showOk, setShowOk] = useState(false)
  const [review, setReview] = useState(null)        // null = closed; { item } = open (item: start on its list)
  const seq = useRef(0)
  const cb = useRef(onLoaded)
  cb.current = onLoaded

  const load = useCallback(async () => {
    const my = ++seq.current
    setLoading(true)
    try {
      const { data: r } = await sz1API.readiness(scope)
      if (my !== seq.current) return
      const d = r?.data || {}
      setRes(d); setErr(null)
      if ((d.block || []).length > 0) setOpen(true)
      cb.current?.(d)
    } catch (e) {
      if (my !== seq.current) return
      setRes(null); setErr(errText(e, 'no answer')); setOpen(true)
      cb.current?.(null)
    } finally {
      if (my === seq.current) setLoading(false)
    }
  }, [scope])

  useEffect(() => { load() }, [load, refreshKey])
  useEffect(() => {
    const on = e => setOpen(!e.detail.collapsed)
    window.addEventListener(FOLD_EVENT, on)
    return () => window.removeEventListener(FOLD_EVENT, on)
  }, [])

  const items = res?.items || []
  const nBlock = res ? (res.block || []).length || items.filter(i => i.level === 'block').length : 0
  const nWarn = res ? (res.warn || []).length || items.filter(i => i.level === 'warn').length : 0
  const summary = loading && !res && !err ? <Chip tone="indigo"><Loader2 size={11} className="animate-spin" style={{ verticalAlign: -1 }} /> Checking…</Chip>
    : err ? <Chip>Not checked</Chip>
    : nBlock ? <Chip tone="red">Blocked{nBlock > 1 ? ` · ${nBlock}` : ''}</Chip>
    : nWarn ? <Chip tone="amber">{nWarn} warning{nWarn > 1 ? 's' : ''}</Chip>
    : res ? <Chip tone="green">Ready</Chip> : null
  const accent = err ? C.cardBorder : nBlock ? C.red : nWarn ? C.amber : res ? C.green : C.cardBorder

  const known = GROUPS.map(g => g.key)
  const groups = [...GROUPS, ...[...new Set(items.map(i => i.group))].filter(g => g && !known.includes(g)).map(g => ({ key: g, name: g, help: null }))]
    .map(g => ({ ...g, items: items.filter(i => i.group === g.key) }))
    .filter(g => g.items.length)
  const toggle = () => setOpen(o => !o)
  const openReview = (item = null) => setReview({ item })
  const closeReview = useCallback(() => setReview(null), [])
  const reviewTip = { title: 'Review readiness', body: 'Open every check in a popup: Blocks, Warnings, Info and Passed as filters, what to do for each, and the list of stores, sessions or tables behind a count.' }
  const iconBtn = { width: 24, height: 24, borderRadius: 7, border: `1px solid ${C.cardBorder}`, background: C.card, cursor: 'pointer',
                    display: 'grid', placeItems: 'center', color: C.textSub, flex: '0 0 24px' }

  return (
    <section style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 14, minWidth: 0,
                      boxShadow: '0 1px 3px rgba(15,23,42,.05)', borderLeft: `4px solid ${accent}` }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '8px 14px', borderBottom: open ? `1px solid ${C.cardBorder}` : 'none' }}>
        <Tip tip={open ? 'Collapse the checklist' : 'Show the checklist'}>
          <button type="button" onClick={toggle} aria-expanded={open} aria-label={open ? 'Collapse' : 'Expand'} style={iconBtn}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        </Tip>
        <span style={{ width: 26, height: 26, borderRadius: 8, background: C.primaryLt, color: C.primary, display: 'grid', placeItems: 'center' }}><ShieldCheck size={14} /></span>
        <Tip tip={reviewTip}>
          <h2 onClick={() => openReview()} style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0, cursor: 'pointer', userSelect: 'none' }}>Readiness</h2>
        </Tip>
        <InfoTip tip={{ title: 'Readiness', body: 'Checked before anything is written. Red blocks the step; amber lets it go ahead and is saved with the build or session. The same check runs again when you start. Click the title or the summary to review every check.' }} />
        {summary && (
          <Tip tip={reviewTip}>
            <button type="button" onClick={() => openReview()} aria-label="Review readiness"
              style={{ border: 'none', background: 'none', padding: 0, cursor: 'pointer', display: 'inline-flex' }}>{summary}</button>
          </Tip>
        )}
        {!open && res && (nBlock || nWarn) > 0 && (
          <span style={{ fontSize: 12, color: C.textSub, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {(nBlock ? res.block : res.warn)?.[0] || ''}
          </span>
        )}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5, color: C.textMuted, whiteSpace: 'nowrap' }}>
          {open && res && (
            <button type="button" onClick={() => setShowOk(v => !v)}
              style={{ fontSize: 11.5, fontWeight: 600, color: C.primary, background: 'none', border: 'none', cursor: 'pointer' }}>
              {showOk ? 'Hide passed checks' : 'Show passed checks'}
            </button>
          )}
          {res?.checked_at && <span>checked {when(res.checked_at)}</span>}
          <Tip tip={reviewTip}>
            <button type="button" onClick={() => openReview()} disabled={!res && !err}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 5, height: 24, padding: '0 9px', borderRadius: 7, cursor: 'pointer',
                       border: `1px solid ${C.cardBorder}`, background: C.card, color: C.primary, fontSize: 11.5, fontWeight: 700 }}>
              <ListChecks size={13} /> Review
            </button>
          </Tip>
          <Tip tip="Check again">
            <button type="button" onClick={load} disabled={loading} aria-label="Check readiness again" style={iconBtn}>
              <RefreshCw size={13} className={loading ? 'animate-spin' : undefined} />
            </button>
          </Tip>
        </div>
      </header>

      {open && (
        <div style={{ padding: 12 }}>
          {err && <div style={{ fontSize: 12, color: C.textMuted }}>Could not check readiness: {err}</div>}
          {!err && res && !groups.length && <div style={{ fontSize: 12, color: C.textMuted }}>No checks returned.</div>}
          {!err && groups.length > 0 && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 10 }}>
              {groups.map(g => {
                const bad = g.items.filter(i => i.level === 'block').length
                const warn = g.items.filter(i => i.level === 'warn').length
                return (
                  <div key={g.key} style={{ border: `1px solid ${bad ? C.redBd : warn ? C.amberBd : C.cardBorder}`, borderRadius: 10, padding: '8px 10px', minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, fontWeight: 800, color: C.textSub,
                                  textTransform: 'uppercase', letterSpacing: '.05em', marginBottom: 6 }}>
                      {g.name}{g.help && <InfoTip tip={{ title: g.name, body: g.help }} size={12} />}
                      <span style={{ marginLeft: 'auto', textTransform: 'none', letterSpacing: 0, fontWeight: 600, color: bad ? C.red : warn ? C.amber : C.green }}>
                        {bad ? `${bad} blocking` : warn ? `${warn} warning${warn > 1 ? 's' : ''}` : 'all ok'}
                      </span>
                    </div>
                    <div style={{ display: 'grid', gap: 6 }}>
                      {!showOk && !bad && !warn && (
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, color: C.textMuted }}>
                          <Dot level="ok" /> {g.items.length} check{g.items.length > 1 ? 's' : ''} passed
                        </div>
                      )}
                      {g.items.filter(it => showOk || it.level !== 'ok').map(it => {
                        const act = it.level !== 'ok' && action ? action(it) : null
                        return (
                          <div key={it.key} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12 }}>
                            <Dot level={it.level} />
                            <div style={{ minWidth: 0, flex: 1 }}>
                              <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                                <span style={{ fontWeight: 600, color: C.text }}>{it.name}</span>
                                {it.value != null && it.value !== '' && (
                                  <span style={{ marginLeft: 'auto', color: C.textSub, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>{it.value}</span>
                                )}
                              </div>
                              {it.detail && <div style={{ color: it.level === 'ok' ? C.textMuted : C.textSub, lineHeight: 1.45, marginTop: 1 }}>{it.detail}</div>}
                              {(act || (it.has_rows && it.level !== 'ok')) && (
                                <div style={{ marginTop: 5, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                                  {act}
                                  {it.has_rows && it.level !== 'ok' && (
                                    <Tip tip="Open the rows behind this check in the review — sort, filter, export">
                                      <button type="button" onClick={() => openReview(it)}
                                        style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11.5, fontWeight: 600, color: C.primary,
                                                 background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
                                        <List size={12} /> View list
                                      </button>
                                    </Tip>
                                  )}
                                </div>
                              )}
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
      <Sz1ReadinessReview open={!!review} onClose={closeReview} startItem={review?.item} scope={scope} res={res}
        loading={loading} onRecheck={load} action={action} />
    </section>
  )
}
