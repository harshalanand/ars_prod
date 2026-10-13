import { useEffect, useRef, useState, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { Check, XCircle, Loader2, ChevronDown, ChevronRight, Info, ArrowRight, ArrowDown, ArrowUp, ArrowUpDown, Ruler, RefreshCw,
  ChevronsDownUp, ChevronsUpDown, Filter, Download, X, LayoutDashboard, Layers, Cpu, ClipboardCheck } from 'lucide-react'
import { C } from '@/theme/colors'
import { COL_HELP } from './sz1Help'
import { useSz1Access } from './sz1Access'

// The 1SZ ALC design kit (spec: public/docs/manual/sz1_alloc.md).
// Every interactive control carries a hover tip; every table header with a
// known column explains itself on hover (COL_HELP).

export const n = v => (v == null || v === '' ? '—' : Math.round(+v).toLocaleString())
export const when = s => (s ? String(s).slice(0, 16).replace('T', ' ') : '—')
export const pct = (a, b) => (b ? `${((a / b) * 100).toFixed(1)}%` : '—')
export const errText = (e, fb) => e?.response?.data?.detail || e?.message || fb

export const MODE_WORDS = { weighted: 'Balanced', mj_srt: 'Biggest total shortage', sequence: 'Own sequence' }
// every run uses live stock since phase 3; "initial" / "balance" only label older sessions
export const BASIS_WORDS = { live: 'Live stock', initial: 'Full stock (older run)', balance: 'Remaining stock (older run)' }
export const CROSS_WORDS = { all: 'All RDC pool', same: 'Own RDC', home_first: 'Home first', pooled: 'Pooled (legacy)', fallback: 'Fallback' }

const INK = '#0f172a'
const GRAD = 'linear-gradient(120deg, #1e1b4b 0%, #3730a3 42%, #6d28d9 100%)'
const TONES = { indigo: [C.primary, C.primaryLt], green: [C.green, C.greenBg], amber: [C.amber, C.amberBg], red: [C.red, C.redBg],
                blue: [C.blue, C.blueBg], slate: [C.textSub, '#f1f5f9'] }

/** Download rows (array of objects) as a CSV file. cols = optional [key | [key, label]]. */
export function downloadCsv(rows, filename, cols) {
  if (!rows?.length) return
  const keys = [...new Set(rows.flatMap(r => Object.keys(r)))]      // union: rows may differ
  const spec = (cols || keys).map(c => (Array.isArray(c) ? c : [c, c]))
  const cell = v => {
    if (v == null) return ''
    const s = String(v)
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const csv = [spec.map(([, l]) => cell(l)).join(','), ...rows.map(r => spec.map(([k]) => cell(r[k])).join(','))].join('\r\n')
  const url = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' }))
  const a = document.createElement('a'); a.href = url; a.download = filename; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 5000)
}

// ── hover tip ──────────────────────────────────────────────────────────────
/** tip: string | { title, body }. Rendered in a portal so table scroll areas never clip it. */
export function Tip({ tip, children, block = false, width = 280 }) {
  const ref = useRef(null)
  const [pos, setPos] = useState(null)
  if (!tip) return children
  const t = typeof tip === 'string' ? { body: tip } : tip
  const show = () => {
    const r = ref.current?.getBoundingClientRect()
    if (r) setPos({ x: Math.min(Math.max(r.left + r.width / 2, width / 2 + 8), window.innerWidth - width / 2 - 8), top: r.top, bottom: r.bottom })
  }
  const below = pos && pos.top < 140
  return (
    <span ref={ref} onMouseEnter={show} onMouseLeave={() => setPos(null)} onFocus={show} onBlur={() => setPos(null)}
      style={{ display: block ? 'block' : 'inline-flex', minWidth: 0 }}>
      {children}
      {pos && createPortal(
        <div role="tooltip" style={{
          position: 'fixed', left: pos.x, top: below ? pos.bottom + 8 : pos.top - 8, zIndex: 9999,
          transform: `translate(-50%, ${below ? '0' : '-100%'})`, width, pointerEvents: 'none',
          background: INK, color: '#e2e8f0', borderRadius: 10, padding: '9px 11px', fontSize: 11.5, lineHeight: 1.5,
          boxShadow: '0 12px 32px rgba(15,23,42,.28)', textAlign: 'left', fontWeight: 400, textTransform: 'none', letterSpacing: 0,
        }}>
          {t.title && <div style={{ fontWeight: 700, color: '#fff', marginBottom: 3 }}>{t.title}</div>}
          <div>{t.body}</div>
        </div>, document.body)}
    </span>
  )
}

export function InfoTip({ tip, size = 13 }) {
  return (
    <Tip tip={tip}>
      <span tabIndex={0} style={{ display: 'inline-grid', placeItems: 'center', cursor: 'help', color: C.textMuted }}><Info size={size} /></span>
    </Tip>
  )
}

// ── page header: title + flow rail + actions on one row; tabs / numbers below ──
// The rail follows the work: Overview · Prepare · Plan a run · Sessions.
// Older page keys (base / req / gap) light up Prepare.
export const STEPS = [
  { key: 'overview', name: 'Overview', path: '/sz1-alloc', icon: LayoutDashboard,
    tip: 'Where things stand and the one thing to do next.' },
  { key: 'prepare', name: 'Prepare', path: '/sz1-alloc/prepare', icon: Layers,
    tip: 'Build Supply (the broken-size candidates) and Demand (each store’s size shortage), and check the CONT gap.' },
  { key: 'run', name: 'Plan a run', path: '/sz1-alloc/run', icon: Cpu,
    tip: 'Choose stores and options; allocate whole packs as a session.' },
  { key: 'sessions', name: 'Sessions', path: '/sz1-alloc/sessions', icon: ClipboardCheck,
    tip: 'Every run with its checks, fill per store and lines — export or delete.' },
]
const STEP_ALIAS = { base: 'prepare', req: 'prepare', gap: 'prepare' }

/** Age of a timestamp in plain words: "just now", "25 min ago", "3 h ago", "2 d ago". */
export function ago(s) {
  if (!s) return '—'
  const t = new Date(String(s).replace(' ', 'T')).getTime()
  if (isNaN(t)) return when(s)
  const m = Math.round((Date.now() - t) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`
  return `${Math.round(m / 1440)} d ago`
}

function stepState(status) {
  const s1 = status?.step1
  const s2 = status?.step2
  const has = status?.balance_sessions?.length
  const kind = status?.running?.kind
  const behind = s1 && (!s2 || +s2.BUILD_ID < +s1.BUILD_ID)
  return {
    note: {
      overview: 'the module at a glance',
      prepare: !s1 ? 'Supply not built yet'
        : `Supply #${s1.BUILD_ID}${s2 ? ` · Demand #${s2.BUILD_ID}` : ' · Demand not built'}${behind && s2 ? ' (older than Supply)' : ''}`,
      run: kind === 'run' ? `session ${status.running.id} running` : 'choose stores and run',
      sessions: has ? `stock held by session ${status.balance_sessions[0]}` : 'no session holds stock',
    },
    state: {
      overview: 'none',
      prepare: kind === 'build1' || kind === 'build2' ? 'running' : !s1 ? 'todo' : behind ? 'warn' : 'done',
      run: kind === 'run' ? 'running' : has ? 'done' : 'todo',
      sessions: has ? 'done' : 'todo',
    },
  }
}

const iconBtn = { width: 30, height: 30, borderRadius: 9, border: `1px solid ${C.cardBorder}`, background: '#fff', cursor: 'pointer',
                  display: 'grid', placeItems: 'center', color: C.textSub, flex: '0 0 30px' }

/**
 * The one header of every 1SZ ALC page (one row, ~52 px): module + title (its
 * description in a hover tip), the flow rail, Collapse / Expand all, refresh.
 * Optional second row: tabs (left) and key numbers (right).
 *   status — GET /sz1/status (or alike), colours the rail.
 *   tabs   — [{ key, label, icon, tip, badge: { text, tone } }], with tab + onTab.
 */
export function Sz1Header({ current, title, subtitle, icon: Icon = Ruler, status, stats = [], onRefresh, refreshTip, extra,
                            tabs, tab, onTab }) {
  const nav = useNavigate()
  const access = useSz1Access()
  const { note, state } = stepState(status)
  const cur = STEP_ALIAS[current] || current
  const pathOf = s => (s.key === 'prepare' ? access.prepareHref || s.path : s.path)
  const hasRow2 = (tabs && tabs.length > 0) || stats.length > 0
  return (
    <section style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 14, overflow: 'hidden',
                      boxShadow: '0 1px 3px rgba(15,23,42,.06)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', padding: '8px 12px',
                    background: 'linear-gradient(90deg, #eef2ff 0%, #f5f3ff 26%, #ffffff 60%)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0, flex: '0 1 auto' }}>
          <span style={{ width: 34, height: 34, borderRadius: 10, background: GRAD, color: '#fff', display: 'grid', placeItems: 'center',
                         flex: '0 0 34px', boxShadow: '0 4px 12px rgba(79,70,229,.28)' }}><Icon size={17} /></span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 10, fontWeight: 800, letterSpacing: '.1em', textTransform: 'uppercase', color: C.primary, lineHeight: 1.2 }}>1SZ ALC</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <h1 style={{ fontSize: 16, fontWeight: 800, color: INK, margin: 0, whiteSpace: 'nowrap', letterSpacing: '-.01em', lineHeight: 1.3 }}>{title}</h1>
              {subtitle && <InfoTip tip={{ title, body: subtitle }} size={13} />}
            </div>
          </div>
        </div>

        <nav aria-label="1SZ ALC flow" style={{ flex: '1 1 420px', display: 'flex', alignItems: 'center', minWidth: 0, overflowX: 'auto' }}>
          {STEPS.map((s, i) => {
            const now = s.key === cur
            const st = state[s.key]
            const col = now ? C.primary : st === 'done' ? C.green : st === 'running' ? C.indigo : st === 'warn' ? C.amber : '#cbd5e1'
            const filled = now || st === 'done' || st === 'running' || st === 'warn'
            const SIcon = s.icon
            return (
              <Fragment key={s.key}>
                <Tip tip={{ title: s.name, body: `${s.tip} Now: ${note[s.key]}.` }}>
                  <button type="button" onClick={() => nav(pathOf(s))} aria-current={now ? 'page' : undefined}
                    style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 10px 4px 4px', borderRadius: 999, cursor: 'pointer',
                             border: `1px solid ${now ? C.primary : 'transparent'}`, background: now ? '#fff' : 'transparent',
                             boxShadow: now ? '0 2px 8px rgba(79,70,229,.16)' : 'none', whiteSpace: 'nowrap' }}>
                    <span style={{ width: 20, height: 20, borderRadius: '50%', display: 'grid', placeItems: 'center', flex: '0 0 20px',
                                   border: `1.5px solid ${col}`, background: filled ? col : '#fff', color: filled ? '#fff' : C.textSub }}>
                      {st === 'running' && !now ? <Loader2 size={11} className="animate-spin" />
                        : st === 'done' && !now ? <Check size={11} strokeWidth={3} /> : <SIcon size={11} />}
                    </span>
                    <span style={{ fontSize: 12, fontWeight: now ? 800 : 600, color: now ? C.primary : C.text }}>{s.name}</span>
                  </button>
                </Tip>
                {i < STEPS.length - 1 && (
                  <span style={{ flex: '1 1 10px', minWidth: 8, maxWidth: 48, height: 2, borderRadius: 2, margin: '0 2px',
                                 background: st === 'done' ? C.greenBd : '#e2e8f0' }} />
                )}
              </Fragment>
            )
          })}
        </nav>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 'auto' }}>
          {extra}
          <Tip tip="Collapse every section on this page to its header">
            <button type="button" onClick={() => foldAll(true)} aria-label="Collapse all" style={iconBtn}><ChevronsDownUp size={14} /></button>
          </Tip>
          <Tip tip="Expand every section on this page">
            <button type="button" onClick={() => foldAll(false)} aria-label="Expand all" style={iconBtn}><ChevronsUpDown size={14} /></button>
          </Tip>
          {onRefresh && (
            <Tip tip={refreshTip || 'Reload'}>
              <button type="button" onClick={onRefresh} aria-label="Refresh" style={iconBtn}><RefreshCw size={14} /></button>
            </Tip>
          )}
        </div>
      </div>

      {hasRow2 && (
        <div style={{ display: 'flex', alignItems: 'stretch', flexWrap: 'wrap', borderTop: `1px solid ${C.cardBorder}`, background: '#fff', minHeight: 36 }}>
          {tabs?.length > 0 && (
            <div role="tablist" style={{ display: 'flex', alignItems: 'stretch', gap: 2, padding: '0 6px', minWidth: 0, overflowX: 'auto' }}>
              {tabs.map(t => {
                const on = t.key === tab
                const TIcon = t.icon
                const [bfg, bbg] = TONES[t.badge?.tone] || TONES.slate
                return (
                  <Tip key={t.key} tip={t.tip}>
                    <button type="button" role="tab" aria-selected={on} onClick={() => onTab && onTab(t.key)}
                      style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '0 11px', height: '100%', minHeight: 36, cursor: 'pointer',
                               border: 'none', background: 'none', whiteSpace: 'nowrap', fontSize: 12.5, fontWeight: on ? 800 : 600,
                               color: on ? C.primary : C.textSub, boxShadow: on ? `inset 0 -2.5px 0 ${C.primary}` : 'none' }}>
                      {TIcon && <TIcon size={14} />}
                      {t.label}
                      {t.badge && <span style={{ fontSize: 10.5, fontWeight: 700, color: bfg, background: bbg, borderRadius: 999, padding: '1px 7px',
                                                 fontVariantNumeric: 'tabular-nums' }}>{t.badge.text}</span>}
                    </button>
                  </Tip>
                )
              })}
            </div>
          )}
          {stats.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', marginLeft: tabs?.length ? 'auto' : 0, minWidth: 0 }}>
              {stats.map((s, i) => (
                <Tip key={s.k} tip={s.tip}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, padding: '6px 12px', cursor: s.tip ? 'help' : 'default', whiteSpace: 'nowrap',
                                borderLeft: i || tabs?.length ? `1px solid ${C.cardBorder}` : 'none' }}>
                    <span style={{ fontSize: 11.5, color: C.textSub, fontWeight: 600 }}>{s.k}</span>
                    <span style={{ fontSize: 13.5, fontWeight: 800, color: s.tone ? (TONES[s.tone] || TONES.slate)[0] : INK,
                                   fontVariantNumeric: 'tabular-nums' }}>{s.v}</span>
                    {s.x && <span style={{ fontSize: 11, color: C.textMuted }}>{s.x}</span>}
                  </div>
                </Tip>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/** A grey placeholder bar while something loads. */
export function Skeleton({ w = '100%', h = 14, r = 6, style }) {
  return (
    <span aria-hidden="true" className="sz1-skel"
      style={{ display: 'block', width: w, height: h, borderRadius: r, ...style }}>
      <style>{'.sz1-skel{background:linear-gradient(90deg,#f1f5f9 25%,#e2e8f0 37%,#f1f5f9 63%);background-size:400% 100%;animation:sz1-skel 1.3s ease infinite}@keyframes sz1-skel{0%{background-position:100% 50%}100%{background-position:0 50%}}'}</style>
    </span>
  )
}

// ── containers ─────────────────────────────────────────────────────────────
// ── collapse / expand ──────────────────────────────────────────────────────
// Every titled panel folds to its header. The state is remembered per panel
// (browser storage; a blocked store just means it isn't remembered), and the
// page header's Collapse all / Expand all reach every panel on the page.
const FOLD_EVENT = 'sz1-panels'
const foldKey = k => {                  // per page and tab: Supply and Demand both have "Build"
  const tab = new URLSearchParams(window.location.search).get('tab')
  return `sz1.panel.${window.location.pathname}${tab ? `.${tab}` : ''}.${k}`
}
const readFold = k => { try { return localStorage.getItem(foldKey(k)) === '1' } catch { return false } }
const writeFold = (k, v) => { try { localStorage.setItem(foldKey(k), v ? '1' : '0') } catch { /* not remembered */ } }
export const foldAll = collapsed => window.dispatchEvent(new CustomEvent(FOLD_EVENT, { detail: { collapsed } }))

/**
 * collapseKey — stable id for remembering the fold state; default = the
 * title without numbers (titles like "What build #8 wrote" keep one key).
 * collapsible={false} — never folds (e.g. a panel whose header holds the page's main action).
 */
export function Panel({ title, icon: Icon, tip, right, children, pad = 16, accent, sticky, collapseKey, collapsible = true }) {
  const key = collapseKey || (typeof title === 'string' ? title.replace(/[#\d]+/g, '').replace(/\s+/g, ' ').trim() : null)
  const canFold = !!(title && collapsible && key)
  const [folded, setFolded] = useState(() => (canFold ? readFold(key) : false))
  useEffect(() => {
    if (!canFold) return undefined
    const on = e => { setFolded(e.detail.collapsed); writeFold(key, e.detail.collapsed) }
    window.addEventListener(FOLD_EVENT, on)
    return () => window.removeEventListener(FOLD_EVENT, on)
  }, [canFold, key])
  const toggle = () => { const v = !folded; setFolded(v); writeFold(key, v) }
  return (
    <section style={{ background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 14, minWidth: 0,
                      ...(sticky && !folded ? { position: 'sticky', top: 8, zIndex: 2 } : {}),
                      boxShadow: '0 1px 3px rgba(15,23,42,.05)', borderTop: accent ? `3px solid ${accent}` : undefined }}>
      {title && (
        <header style={{ display: 'flex', alignItems: 'center', gap: 9, padding: folded ? '9px 16px' : '12px 16px',
                         borderBottom: folded ? 'none' : `1px solid ${C.cardBorder}` }}>
          {canFold && (
            <Tip tip={folded ? 'Expand this section' : 'Collapse this section'}>
              <button type="button" onClick={toggle} aria-expanded={!folded} aria-label={folded ? 'Expand' : 'Collapse'}
                style={{ width: 24, height: 24, borderRadius: 7, border: `1px solid ${C.cardBorder}`, background: '#fff', cursor: 'pointer',
                         display: 'grid', placeItems: 'center', color: C.textSub, flex: '0 0 24px' }}>
                {folded ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
              </button>
            </Tip>
          )}
          {Icon && <span style={{ width: 28, height: 28, borderRadius: 8, background: C.primaryLt, color: C.primary, display: 'grid', placeItems: 'center' }}><Icon size={15} /></span>}
          <h2 onClick={canFold ? toggle : undefined}
            style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0, cursor: canFold ? 'pointer' : 'default', userSelect: 'none' }}>{title}</h2>
          {tip && <InfoTip tip={tip} />}
          {folded && <span style={{ fontSize: 11, color: C.textMuted }}>collapsed</span>}
          {right && <div style={{ marginLeft: 'auto', fontSize: 12, color: C.textMuted, display: 'flex', alignItems: 'center', gap: 8 }}>{right}</div>}
        </header>
      )}
      {!folded && <div style={{ padding: pad }}>{children}</div>}
    </section>
  )
}

export function Banner({ tone = 'amber', label, text, action }) {
  const t = { red: [C.redBg, C.redBd, C.red], amber: [C.amberBg, C.amberBd, C.amber], green: [C.greenBg, C.greenBd, C.green],
              blue: [C.blueBg, C.blueBd, C.blue] }[tone]
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, background: t[0], border: `1px solid ${t[1]}`, borderRadius: 12, padding: '10px 14px' }}>
      <span style={{ fontSize: 11, fontWeight: 800, color: '#fff', background: t[2], borderRadius: 6, padding: '3px 8px', flex: '0 0 auto' }}>{label}</span>
      <span style={{ fontSize: 12.5, color: C.text, lineHeight: 1.5 }}>{text}</span>
      {action && <span style={{ marginLeft: 'auto', flex: '0 0 auto' }}>{action}</span>}
    </div>
  )
}

export function Kpi({ k, v, x, tone = 'slate', icon: Icon, tip }) {
  const [fg, bg] = TONES[tone] || TONES.slate
  return (
    <Tip tip={tip} block>
      <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: '11px 13px', background: C.card, minWidth: 0,
                    display: 'flex', gap: 10, alignItems: 'flex-start', cursor: tip ? 'help' : 'default' }}>
        {Icon && <span style={{ width: 32, height: 32, borderRadius: 9, background: bg, color: fg, display: 'grid', placeItems: 'center', flex: '0 0 32px' }}><Icon size={16} /></span>}
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 11.5, color: C.textSub, fontWeight: 600 }}>{k}</div>
          <div style={{ fontSize: 20, fontWeight: 800, color: tone === 'slate' ? C.text : fg, fontVariantNumeric: 'tabular-nums', lineHeight: 1.2 }}>{v}</div>
          {x && <div style={{ fontSize: 11, color: C.textMuted, marginTop: 1 }}>{x}</div>}
        </div>
      </div>
    </Tip>
  )
}

export function Sub({ children, tip }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 700, color: C.textSub, margin: '4px 0 8px' }}>
      {children}{tip && <InfoTip tip={tip} size={12} />}
    </div>
  )
}

export function Badge({ s }) {
  const t = { DONE: [C.green, C.greenBg], RUNNING: [C.indigo, C.indigoBg], FAILED: [C.red, C.redBg],
              CANCELLED: [C.gray, C.grayBg] }[s] || [C.gray, C.grayBg]
  return <span style={{ fontSize: 11, fontWeight: 700, color: t[0], background: t[1], borderRadius: 999, padding: '3px 10px' }}>{s}</span>
}

/** Primary / secondary buttons with a hover tip. */
export function Btn({ kind = 'secondary', icon: Icon, children, tip, busy, ...rest }) {
  return (
    <Tip tip={tip}>
      <button type="button" className={kind === 'primary' ? 'btn-primary' : 'btn-secondary'} {...rest}
        style={{ display: 'inline-flex', alignItems: 'center', gap: 6, ...(rest.style || {}) }}>
        {busy ? <Loader2 size={13} className="animate-spin" /> : Icon ? <Icon size={13} /> : null}{children}
      </button>
    </Tip>
  )
}

// ── option tiles (the run's choices) ───────────────────────────────────────
export function OptionTiles({ value, onChange, options, cols }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: `repeat(${cols || options.length}, minmax(0, 1fr))`, gap: 8 }}>
      {options.map(o => {
        const on = value === o.key
        const Icon = o.icon
        return (
          <Tip key={o.key} tip={{ title: o.name, body: o.help }} block>
            <button type="button" onClick={() => onChange(o.key)} aria-pressed={on}
              style={{ width: '100%', textAlign: 'left', cursor: 'pointer', borderRadius: 12, padding: '11px 12px', position: 'relative',
                       background: on ? C.primaryLt : C.card, border: `1.5px solid ${on ? C.primary : C.cardBorder}`,
                       boxShadow: on ? '0 4px 14px rgba(79,70,229,.15)' : 'none', transition: 'all .15s' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {Icon && <span style={{ width: 28, height: 28, borderRadius: 8, display: 'grid', placeItems: 'center',
                                        background: on ? C.primary : '#f1f5f9', color: on ? '#fff' : C.textSub }}><Icon size={15} /></span>}
                <span style={{ fontSize: 13, fontWeight: 700, color: on ? C.primary : C.text }}>{o.name}</span>
                {o.badge && <span style={{ marginLeft: 'auto', fontSize: 10, fontWeight: 800, color: C.green, background: C.greenBg, borderRadius: 999, padding: '2px 7px' }}>{o.badge}</span>}
              </div>
              {o.tag && <div style={{ fontSize: 11.5, color: C.textSub, marginTop: 5, lineHeight: 1.4 }}>{o.tag}</div>}
            </button>
          </Tip>
        )
      })}
    </div>
  )
}

/** Sticky bottom bar for a page's main action. */
export function ActionBar({ children }) {
  return (
    <div style={{ position: 'sticky', bottom: 8, zIndex: 20, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
                  background: 'rgba(255,255,255,.96)', backdropFilter: 'blur(6px)', border: `1px solid ${C.cardBorder}`,
                  borderRadius: 14, padding: '10px 14px', boxShadow: '0 10px 30px rgba(15,23,42,.12)' }}>
      {children}
    </div>
  )
}

export function Chip({ children, tone = 'slate' }) {
  const [fg, bg] = TONES[tone] || TONES.slate
  return <span style={{ fontSize: 11.5, fontWeight: 600, color: fg, background: bg, borderRadius: 999, padding: '3px 10px', whiteSpace: 'nowrap' }}>{children}</span>
}

// ── table ──────────────────────────────────────────────────────────────────
const isNum = v => typeof v === 'number' || (typeof v === 'string' && v !== '' && !isNaN(+v) && /^-?\d+(\.\d+)?$/.test(v))
// codes that happen to be digits (articles, session ids, sizes) are shown as written
const CODE_COL = /^(ARTICLE_NUMBER|GEN_ART_NUMBER|OPT_KEY|SESSION_ID|BUILD_ID|ST_CD|SZ|K|CLR|RDC|SRC_RDC|RULES)$/i
// CONT % columns hold 0–100 values: shown as a percentage with 2 decimals
export const PCT_COL = /(CONT_PCT|^(MIN|MAX)_PCT$)/i
export const pct2 = v => `${(+v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`

// ── column filters (Excel style) ───────────────────────────────────────────
// A filter is { values: [..] } (the value is one of these; null = blank) or
// { expr: '..' } (numbers: >10 <=5 =0 <>0 10-20; text: contains). The same
// shape goes to the server as `cf`, so client and server filter alike.
const BLANK = '\u0000blank'
const keyOf = v => (v == null || v === '' ? BLANK : String(v))

function exprPasses(v, t, isNumCol) {
  t = String(t).trim()
  if (!t) return true
  if (isNumCol) {
    const x = v == null ? NaN : +v
    let m = t.match(/^(>=|<=|<>|>|<|=)\s*(-?\d+(\.\d+)?)$/)
    if (m) {
      const y = +m[2]
      return { '>': x > y, '<': x < y, '>=': x >= y, '<=': x <= y, '=': x === y, '<>': x !== y }[m[1]]
    }
    m = t.match(/^(-?\d+(\.\d+)?)\s*-\s*(-?\d+(\.\d+)?)$/)
    if (m) return x >= +m[1] && x <= +m[3]
    if (/^-?\d+(\.\d+)?$/.test(t)) return x === +t
  }
  return String(v ?? '').toUpperCase().includes(t.toUpperCase())
}

function passes(v, f, isNumCol) {
  if (!f) return true
  if (Array.isArray(f.values)) return f.values.some(x => keyOf(x) === keyOf(v))
  return exprPasses(v, f.expr, isNumCol)
}

const filterWords = f => {
  if (!f) return ''
  if (f.expr != null) return f.expr
  const vs = f.values.map(v => (keyOf(v) === BLANK ? '(blank)' : String(v)))
  return vs.length > 2 ? `${vs.slice(0, 2).join(', ')} +${vs.length - 2}` : vs.join(', ') || '(none)'
}

/** The drop-down under a column's funnel: distinct values (with counts),
 *  search, select all, and a condition box. load(search) → {values:[{v,n}], distinct, more}. */
function FilterMenu({ col, label, numeric, current, load, anchor, onApply, onClose, onSort, sortDir }) {
  const box = useRef(null)
  const [search, setSearch] = useState('')
  const [expr, setExpr] = useState(current?.expr ?? '')
  const [list, setList] = useState(null)          // {values, distinct, more}
  const [err, setErr] = useState(null)
  const [sel, setSel] = useState(() => (Array.isArray(current?.values) ? new Set(current.values.map(keyOf)) : null))
  const known = useRef(new Map((current?.values || []).map(v => [keyOf(v), v])))

  useEffect(() => {
    let live = true
    const t = setTimeout(() => {
      setErr(null)
      Promise.resolve(load(search.trim()))
        .then(d => { if (!live) return; d.values.forEach(x => known.current.set(keyOf(x.v), x.v)); setList(d) })
        .catch(e => live && setErr(errText(e, 'Could not load the values')))
    }, search ? 250 : 0)
    return () => { live = false; clearTimeout(t) }
  }, [search]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const down = e => { if (box.current && !box.current.contains(e.target) && !e.target.closest?.('[data-sz1-funnel]')) onClose() }
    const key = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('mousedown', down); window.addEventListener('keydown', key); window.addEventListener('resize', onClose)
    return () => { window.removeEventListener('mousedown', down); window.removeEventListener('keydown', key); window.removeEventListener('resize', onClose) }
  }, [onClose])

  const shown = list?.values || []
  const isOn = k => sel == null || sel.has(k)
  const toggle = k => setSel(s => {
    const next = new Set(s ?? shown.map(x => keyOf(x.v)))
    next.has(k) ? next.delete(k) : next.add(k)
    return next
  })
  const allShownOn = shown.length > 0 && shown.every(x => isOn(keyOf(x.v)))
  const setAllShown = on => setSel(s => {
    if (on && s == null) return null
    const next = new Set(s ?? shown.map(x => keyOf(x.v)))
    shown.forEach(x => (on ? next.add(keyOf(x.v)) : next.delete(keyOf(x.v))))
    return next
  })
  const apply = () => {
    if (String(expr).trim()) return onApply({ expr: String(expr).trim() })
    if (search.trim()) return onApply({ values: shown.filter(x => isOn(keyOf(x.v))).map(x => x.v) })
    if (sel == null || (!list?.more && shown.every(x => sel.has(keyOf(x.v))) && sel.size === shown.length)) return onApply(null)
    return onApply({ values: [...sel].map(k => (k === BLANK ? null : known.current.get(k))) })
  }

  const W = 290
  const left = Math.min(Math.max(anchor.left, 8), window.innerWidth - W - 8)
  const top = Math.min(anchor.bottom + 4, window.innerHeight - 420)
  const fmtV = v => (keyOf(v) === BLANK ? <i style={{ color: C.textMuted }}>(blank)</i>
    : numeric && PCT_COL.test(col) && isNum(v) ? pct2(v)
    : numeric && !CODE_COL.test(col) && isNum(v) ? (+v).toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v))
  const inp = { width: '100%', height: 28, fontSize: 12, padding: '0 8px', borderRadius: 7, border: `1px solid ${C.cardBorder}`, background: '#fff' }

  return createPortal(
    <div ref={box} role="dialog" aria-label={`Filter ${label}`}
      style={{ position: 'fixed', left, top: Math.max(top, 8), width: W, zIndex: 9500, background: C.card, borderRadius: 12,
               border: `1px solid ${C.cardBorder}`, boxShadow: '0 18px 48px rgba(15,23,42,.25)', padding: 10, fontSize: 12 }}>
      <div style={{ fontWeight: 800, color: C.text, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
        <Filter size={13} color={C.primary} /> {label}
        {list && <span style={{ marginLeft: 'auto', fontWeight: 500, color: C.textMuted, fontSize: 11 }}>{list.distinct.toLocaleString()} distinct</span>}
      </div>
      {onSort && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 8 }}>
          {[['asc', ArrowUp, numeric ? 'Smallest first' : 'A → Z'], ['desc', ArrowDown, numeric ? 'Largest first' : 'Z → A']].map(([d, Icon, t]) => (
            <button key={d} type="button" onClick={() => onSort(sortDir === d ? null : d)}
              style={{ height: 28, borderRadius: 7, cursor: 'pointer', fontSize: 11.5, fontWeight: 600, display: 'inline-flex', alignItems: 'center',
                       justifyContent: 'center', gap: 5, border: `1px solid ${sortDir === d ? C.primary : C.cardBorder}`,
                       background: sortDir === d ? C.primaryLt : '#fff', color: sortDir === d ? C.primary : C.textSub }}>
              <Icon size={12} /> {t}
            </button>
          ))}
        </div>
      )}
      <input autoFocus value={search} onChange={e => setSearch(e.target.value)} placeholder="Search values…" style={inp} />
      <div style={{ margin: '8px 0', border: `1px solid ${C.cardBorder}`, borderRadius: 8, maxHeight: 230, overflow: 'auto' }}>
        {err ? <div style={{ padding: 10, color: C.red }}>{err}</div>
          : !list ? <div style={{ padding: 10, color: C.textMuted, display: 'flex', gap: 6, alignItems: 'center' }}><Loader2 size={13} className="animate-spin" /> Loading values…</div>
          : !shown.length ? <div style={{ padding: 10, color: C.textMuted }}>No value matches</div>
          : (
            <>
              <label style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '5px 8px', fontWeight: 700, borderBottom: `1px solid #f1f5f9`, cursor: 'pointer' }}>
                <input type="checkbox" checked={allShownOn} onChange={e => setAllShown(e.target.checked)} />
                {search ? 'Select all search results' : 'Select all'}
              </label>
              {shown.map(x => {
                const k = keyOf(x.v)
                return (
                  <label key={k} style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '4px 8px', cursor: 'pointer' }}>
                    <input type="checkbox" checked={isOn(k)} onChange={() => toggle(k)} />
                    <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{fmtV(x.v)}</span>
                    <span style={{ color: C.textMuted, fontSize: 11 }}>{x.n.toLocaleString()}</span>
                  </label>
                )
              })}
              {list.more && <div style={{ padding: '5px 8px', color: C.textMuted, fontSize: 11 }}>Showing the {shown.length} most common — search to find others.</div>}
            </>
          )}
      </div>
      <div style={{ fontSize: 11, color: C.textSub, marginBottom: 4 }}>{numeric ? 'Or a condition: >10 · <=5 · =0 · <>0 · 10-20' : 'Or text the value contains'}</div>
      <input value={expr} onChange={e => setExpr(e.target.value)} onKeyDown={e => e.key === 'Enter' && apply()}
        placeholder={numeric ? 'e.g. >100' : 'e.g. SHIRT'} style={{ ...inp, textAlign: numeric ? 'right' : 'left' }} />
      <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
        <button type="button" onClick={() => onApply(null)}
          style={{ flex: 1, height: 30, borderRadius: 8, border: `1px solid ${C.cardBorder}`, background: '#fff', cursor: 'pointer', fontWeight: 600, color: C.textSub }}>
          Clear
        </button>
        <button type="button" onClick={apply}
          style={{ flex: 2, height: 30, borderRadius: 8, border: 'none', background: C.primary, color: '#fff', cursor: 'pointer', fontWeight: 700 }}>
          Apply
        </button>
      </div>
    </div>, document.body)
}

const TABLE_CSS = `
.sz1-th .sz1-hbtn { opacity: .28; transition: opacity .12s }
.sz1-th:hover .sz1-hbtn, .sz1-hbtn.on { opacity: 1 }
.sz1-th .sz1-sort { cursor: pointer; user-select: none }
.sz1-th .sz1-sort:hover { color: ${C.primary} }
.sz1-tbtn { display: inline-flex; align-items: center; gap: 5px; height: 26px; padding: 0 10px; border-radius: 7px; font-size: 11.5px;
            font-weight: 600; cursor: pointer; border: 1px solid ${C.cardBorder}; background: #fff; color: ${C.textSub}; white-space: nowrap }
.sz1-tbtn:hover { border-color: ${C.primary}; color: ${C.primary} }
.sz1-tbtn:disabled { opacity: .5; cursor: default }
`

const cmp = (a, b, num) => {
  if (a == null || a === '') return b == null || b === '' ? 0 : 1          // blanks last
  if (b == null || b === '') return -1
  return num ? (+a) - (+b) : String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' })
}

/**
 * rows — array of objects; cols — optional [key | [key, label]].
 * Every table can be sorted (click a header: ascending → descending → off),
 * filtered (funnel: Excel-style distinct values, search, condition) and
 * exported (CSV of what the table shows, in its order). Headers in COL_HELP
 * explain themselves on hover.
 *
 * server — { filters, onChange, distinct(col, search, otherFilters), total,
 *            sort, onSort({col, dir} | null), onExport() }:
 *   the page re-reads rows from the server on every change, so filters and
 *   sorting cover the WHOLE table and export streams every matching row.
 * exportName — CSV file name (client tables). sortable / filterable = false to switch off.
 */
export function DataTable({ rows = [], cols, onRow, maxHeight = 380, empty = 'Nothing to show', selected,
                            filterable = true, sortable = true, server, exportName = '1SZ_table.csv', toolbar = true }) {
  const [own, setOwn] = useState({})
  const [ownSort, setOwnSort] = useState(null)      // { col, dir }
  const [menu, setMenu] = useState(null)            // { k, label, anchor }
  const [exporting, setExporting] = useState(false)
  const lastSpec = useRef(null)
  const lastNum = useRef({})
  const filters = server ? server.filters || {} : own
  const setFilters = server ? server.onChange : setOwn
  const sort = server ? server.sort || null : ownSort
  const setSort = server ? (server.onSort || (() => {})) : setOwnSort
  const canFilter = !!(server || filterable)
  const canSort = !!(sortable && (!server || server.onSort))
  const active = Object.entries(filters).filter(([, f]) => f)

  let spec = null
  if (rows.length) spec = (cols || Object.keys(rows[0])).map(c => (Array.isArray(c) ? c : [c, c]))
  else if (cols) spec = cols.map(c => (Array.isArray(c) ? c : [c, c]))
  else if (active.length) spec = lastSpec.current           // keep the headers so a filter can be undone
  if (!spec) return <div style={{ fontSize: 12, color: C.textMuted, padding: '14px 4px' }}>{empty}</div>
  lastSpec.current = spec
  const numeric = rows.length
    ? Object.fromEntries(spec.map(([k]) => [k, !CODE_COL.test(k) && rows.slice(0, 20).every(r => r[k] == null || isNum(r[k]) || typeof r[k] === 'boolean')]))
    : lastNum.current
  lastNum.current = numeric

  let shown = server || !active.length ? rows : rows.filter(r => active.every(([k, f]) => passes(r[k], f, numeric[k])))
  if (!server && sort) {
    const d = sort.dir === 'desc' ? -1 : 1
    shown = [...shown].sort((a, b) => d * cmp(a[sort.col], b[sort.col], numeric[sort.col]))
  }
  const total = server ? server.total ?? rows.length : rows.length

  const setOne = (k, f) => setFilters({ ...Object.fromEntries(active.filter(([c]) => c !== k)), ...(f ? { [k]: f } : {}) })
  const cycleSort = k => setSort(!sort || sort.col !== k ? { col: k, dir: 'asc' } : sort.dir === 'asc' ? { col: k, dir: 'desc' } : null)
  const loadValues = k => search => {
    const others = Object.fromEntries(active.filter(([c]) => c !== k))
    if (server) return server.distinct(k, search, others)
    const counts = new Map()
    rows.forEach(r => {
      if (!Object.entries(others).every(([c, f]) => passes(r[c], f, numeric[c]))) return
      const v = r[k]
      if (search && !String(v ?? '').toUpperCase().includes(search.toUpperCase())) return
      const key = keyOf(v)
      const e = counts.get(key)
      e ? e.n++ : counts.set(key, { v: key === BLANK ? null : v, n: 1 })
    })
    const values = [...counts.values()].sort((a, b) => (numeric[k] ? cmp(a.v, b.v, true) : b.n - a.n || cmp(a.v, b.v, false)))
    return { values: values.slice(0, 500), distinct: values.length, more: values.length > 500 }
  }
  const openMenu = (e, k, label) => {
    e.stopPropagation()
    const r = e.currentTarget.getBoundingClientRect()
    setMenu(m => (m?.k === k ? null : { k, label, anchor: { left: r.left, bottom: r.bottom } }))
  }
  const doExport = async () => {
    if (!server?.onExport) return downloadCsv(shown, exportName, spec)
    setExporting(true)
    try { await server.onExport() } finally { setExporting(false) }
  }

  const fmt = (k, v) => {
    if (v == null) return <span style={{ color: C.textMuted }}>—</span>
    if (typeof v === 'boolean') return v ? '✓' : '—'
    if (numeric[k] && PCT_COL.test(k)) return pct2(v)
    if (numeric[k]) return Number.isInteger(+v) ? (+v).toLocaleString() : (+v).toLocaleString(undefined, { maximumFractionDigits: 4 })
    return String(v)
  }
  const nice = (k, label) => (label === k ? label.replace(/_/g, ' ') : label)
  const countText = server
    ? `${total.toLocaleString()} ${active.length ? 'matching ' : ''}rows${total > rows.length ? ` · first ${rows.length.toLocaleString()} shown` : ''}`
    : active.length ? `${shown.length.toLocaleString()} of ${rows.length.toLocaleString()} rows` : `${rows.length.toLocaleString()} rows`
  const sortLabel = sort && spec.find(([c]) => c === sort.col)

  return (
    <div>
    <style>{TABLE_CSS}</style>
    {toolbar && (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, minHeight: 28 }}>
        <span style={{ fontSize: 11.5, color: C.textSub, fontWeight: 600, whiteSpace: 'nowrap' }}>{countText}</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flex: 1, minWidth: 0, overflowX: 'auto' }}>
          {sortLabel && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, background: '#f1f5f9', color: C.textSub, borderRadius: 999,
                           padding: '2px 4px 2px 9px', fontSize: 11.5, fontWeight: 600, whiteSpace: 'nowrap' }}>
              {sort.dir === 'desc' ? <ArrowDown size={11} /> : <ArrowUp size={11} />} {nice(sortLabel[0], sortLabel[1])}
              <button type="button" aria-label="Remove sort" onClick={() => setSort(null)}
                style={{ border: 'none', background: 'none', cursor: 'pointer', color: C.textMuted, display: 'grid', placeItems: 'center', padding: 1 }}>
                <XCircle size={12} />
              </button>
            </span>
          )}
          {active.map(([k, f]) => (
            <span key={k} title={filterWords(f)} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, background: C.primaryLt, color: C.primary,
                                   borderRadius: 999, padding: '2px 4px 2px 9px', fontSize: 11.5, fontWeight: 600, whiteSpace: 'nowrap' }}>
              <Filter size={10} /> {nice(k, (spec.find(([c]) => c === k) || [k, k])[1])}: {filterWords(f)}
              <button type="button" aria-label={`Remove filter on ${k}`} onClick={() => setOne(k, null)}
                style={{ border: 'none', background: 'none', cursor: 'pointer', color: C.primary, display: 'grid', placeItems: 'center', padding: 1 }}>
                <XCircle size={12} />
              </button>
            </span>
          ))}
        </div>
        {active.length > 0 && (
          <button type="button" className="sz1-tbtn" onClick={() => setFilters({})}>
            <X size={12} /> Clear filters
          </button>
        )}
        <Tip tip={{ title: 'Export CSV', body: server?.onExport
          ? 'Every matching row of the whole table (not only the rows on screen), every column, in this order. Read only.'
          : 'The rows this table shows — filtered and in this order — as CSV.' }}>
          <button type="button" className="sz1-tbtn" onClick={doExport} disabled={exporting || (!server?.onExport && !shown.length)}>
            {exporting ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />} Export
          </button>
        </Tip>
      </div>
    )}
    <div style={{ overflow: 'auto', maxHeight, border: `1px solid ${C.cardBorder}`, borderRadius: 10 }}>
      <table style={{ width: '100%', borderCollapse: 'separate', borderSpacing: 0, fontSize: 12, fontVariantNumeric: 'tabular-nums' }}>
        <thead style={{ position: 'sticky', top: 0, zIndex: 2, background: '#f8fafc', boxShadow: `0 1px 0 ${C.cardBorder}` }}>
          <tr>
            {spec.map(([k, label]) => {
              const help = COL_HELP[k]
              const on = !!filters[k]
              const sorted = sort?.col === k ? sort.dir : null
              const text = nice(k, label)
              const word = <span style={help ? { borderBottom: `1px dotted ${C.textMuted}` } : undefined}>{text}</span>
              return (
                <th key={k} className="sz1-th" style={{ background: on ? C.primaryLt : '#f8fafc', padding: '7px 8px', whiteSpace: 'nowrap',
                                     fontSize: 11, fontWeight: 700, color: on || sorted ? C.primary : C.textSub, textAlign: numeric[k] ? 'right' : 'left',
                                     borderBottom: `1px solid ${C.cardBorder}` }}>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, flexDirection: numeric[k] ? 'row-reverse' : 'row' }}>
                    <span className={canSort ? 'sz1-sort' : undefined} onClick={canSort ? () => cycleSort(k) : undefined}
                      style={{ display: 'inline-flex', alignItems: 'center', gap: 3, flexDirection: numeric[k] ? 'row-reverse' : 'row' }}>
                      {help ? <Tip tip={{ title: k, body: help }}>{word}</Tip> : word}
                      {canSort && (
                        <span className={`sz1-hbtn${sorted ? ' on' : ''}`} style={{ display: 'grid', placeItems: 'center' }}
                          title={sorted === 'asc' ? 'Sorted ascending — click for descending' : sorted === 'desc' ? 'Sorted descending — click to clear' : 'Click to sort'}>
                          {sorted === 'asc' ? <ArrowUp size={11} /> : sorted === 'desc' ? <ArrowDown size={11} /> : <ArrowUpDown size={11} />}
                        </span>
                      )}
                    </span>
                    {canFilter && (
                      <button type="button" data-sz1-funnel onClick={e => openMenu(e, k, text)} aria-label={`Filter ${text}`}
                        className={`sz1-hbtn${on ? ' on' : ''}`} title={on ? `Filtered: ${filterWords(filters[k])}` : `Filter ${text}`}
                        style={{ border: 'none', cursor: 'pointer', borderRadius: 5, width: 18, height: 18, display: 'grid', placeItems: 'center', padding: 0,
                                 background: on ? C.primary : 'transparent', color: on ? '#fff' : C.textSub }}>
                        <Filter size={11} />
                      </button>
                    )}
                  </span>
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => {
            const sel = selected && selected(r)
            return (
              <tr key={i} onClick={onRow ? () => onRow(r) : undefined} className="sz1-row"
                style={{ cursor: onRow ? 'pointer' : undefined, background: sel ? C.primaryLt : i % 2 ? '#fcfcfd' : C.card }}>
                {spec.map(([k]) => (
                  <td key={k} style={{ padding: '6px 10px', whiteSpace: 'nowrap', textAlign: numeric[k] ? 'right' : 'left', color: C.text,
                                       borderTop: i ? `1px solid #f1f5f9` : 'none', fontWeight: sel && !numeric[k] ? 700 : 400 }}>
                    {fmt(k, r[k])}
                  </td>
                ))}
              </tr>
            )
          })}
          {!shown.length && (
            <tr><td colSpan={spec.length} style={{ padding: 14, color: C.textMuted, fontSize: 12 }}>
              {active.length ? 'No row matches the filters — remove one above.' : empty}
            </td></tr>
          )}
        </tbody>
      </table>
    </div>
    {menu && (
      <FilterMenu key={menu.k} col={menu.k} label={menu.label} numeric={!!numeric[menu.k]} current={filters[menu.k]}
        load={loadValues(menu.k)} anchor={menu.anchor} onClose={() => setMenu(null)}
        sortDir={sort?.col === menu.k ? sort.dir : null}
        onSort={canSort ? dir => { setSort(dir ? { col: menu.k, dir } : null); setMenu(null) } : null}
        onApply={f => { setOne(menu.k, f); setMenu(null) }} />
    )}
    </div>
  )
}

// ── popup window ───────────────────────────────────────────────────────────
/** A centred popup over the page. Closes on Esc, the × or a click outside. */
export function Modal({ open, onClose, title, icon: Icon, width = 1080, children, footer }) {
  useEffect(() => {
    if (!open) return undefined
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])
  if (!open) return null
  return createPortal(
    <div role="dialog" aria-modal="true" aria-label={typeof title === 'string' ? title : undefined}
      onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}
      style={{ position: 'fixed', inset: 0, zIndex: 9000, background: 'rgba(15,23,42,.45)', display: 'grid', placeItems: 'center', padding: 16 }}>
      <div style={{ width: `min(${width}px, 96vw)`, maxHeight: '90vh', display: 'flex', flexDirection: 'column', background: C.card,
                    borderRadius: 16, boxShadow: '0 24px 64px rgba(15,23,42,.35)', overflow: 'hidden' }}>
        <header style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px', borderBottom: `1px solid ${C.cardBorder}` }}>
          {Icon && <span style={{ width: 30, height: 30, borderRadius: 9, background: C.primaryLt, color: C.primary, display: 'grid', placeItems: 'center' }}><Icon size={16} /></span>}
          <h2 style={{ fontSize: 15, fontWeight: 800, color: C.text, margin: 0 }}>{title}</h2>
          <button type="button" onClick={onClose} aria-label="Close"
            style={{ marginLeft: 'auto', width: 30, height: 30, borderRadius: 8, border: `1px solid ${C.cardBorder}`, background: '#fff',
                     cursor: 'pointer', display: 'grid', placeItems: 'center', color: C.textSub }}><XCircle size={16} /></button>
        </header>
        <div style={{ padding: 16, overflow: 'auto' }}>{children}</div>
        {footer && <footer style={{ padding: '10px 16px', borderTop: `1px solid ${C.cardBorder}`, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>{footer}</footer>}
      </div>
    </div>, document.body)
}

/** A clickable "setting label": icon + name + short summary; opens a popup. */
export function SettingLabel({ icon: Icon, label, summary, onClick, tip, tone = 'indigo' }) {
  const [fg, bg] = TONES[tone] || TONES.indigo
  return (
    <Tip tip={tip}>
      <button type="button" onClick={onClick}
        style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '6px 10px', borderRadius: 10, cursor: 'pointer',
                 border: `1px solid ${C.cardBorder}`, background: '#fff', textAlign: 'left', maxWidth: '100%' }}>
        {Icon && <span style={{ width: 26, height: 26, borderRadius: 8, background: bg, color: fg, display: 'grid', placeItems: 'center', flex: '0 0 26px' }}><Icon size={14} /></span>}
        <span style={{ minWidth: 0 }}>
          <span style={{ display: 'block', fontSize: 12.5, fontWeight: 700, color: C.text }}>{label}</span>
          {summary && <span style={{ display: 'block', fontSize: 11, color: C.textSub, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{summary}</span>}
        </span>
        <ChevronRight size={14} color={C.textMuted} />
      </button>
    </Tip>
  )
}

/** The allocation-limit checks of a session: [{key, name, ok, examples}]. */
export function Checks({ items = [] }) {
  const [open, setOpen] = useState(null)
  if (!items.length) return <div style={{ fontSize: 12, color: C.textMuted }}>No checks recorded</div>
  const failed = items.filter(c => !c.ok).length
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 6 }}>
      {items.map(c => (
        <div key={c.key} style={{ border: `1px solid ${c.ok ? C.greenBd : C.redBd}`, background: c.ok ? '#f6fef9' : C.redBg,
                                  borderRadius: 10, padding: '7px 10px', gridColumn: open === c.key ? '1 / -1' : undefined }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, cursor: c.ok ? 'default' : 'pointer' }}
            onClick={() => !c.ok && setOpen(open === c.key ? null : c.key)}>
            <span style={{ width: 20, height: 20, borderRadius: '50%', display: 'grid', placeItems: 'center', flex: '0 0 20px',
                           background: c.ok ? C.green : C.red, color: '#fff' }}>{c.ok ? <Check size={12} strokeWidth={3} /> : <XCircle size={12} />}</span>
            <span style={{ fontWeight: 600, color: C.text }}>{c.name}</span>
            {!c.ok && <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 4, color: C.red, fontWeight: 700, fontSize: 11 }}>
              breached {open === c.key ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>}
          </div>
          {open === c.key && <div style={{ marginTop: 8 }}><DataTable rows={c.examples || []} cols={[['K', 'Where'], ['GOT', 'Got'], ['LIMIT_', 'Limit']]} maxHeight={180} exportName={`1SZ_check_${c.key}.csv`} /></div>}
        </div>
      ))}
      {failed === 0 && <div style={{ gridColumn: '1 / -1', fontSize: 11.5, color: C.green, fontWeight: 600 }}>All {items.length} limits held — zero breaches.</div>}
    </div>
  )
}

// ── flowcharts (Help, and the Step 3 explainer) ────────────────────────────
const NODE = {
  start: { bg: GRAD, fg: '#fff', bd: 'transparent' },
  step: { bg: '#fff', fg: INK, bd: '#c7d2fe' },
  check: { bg: '#fffbeb', fg: '#78350f', bd: '#fcd34d' },
  good: { bg: '#ecfdf5', fg: '#065f46', bd: '#6ee7b7' },
  stop: { bg: '#fef2f2', fg: '#7f1d1d', bd: '#fca5a5' },
  data: { bg: '#eff6ff', fg: '#1e3a8a', bd: '#93c5fd' },
}

export function FlowNode({ kind = 'step', title, body, tip, n: num, width }) {
  const s = NODE[kind] || NODE.step
  return (
    <Tip tip={tip} block>
      <div style={{ background: s.bg, color: s.fg, border: `1.5px solid ${s.bd}`, borderRadius: kind === 'check' ? 999 : 12,
                    padding: kind === 'check' ? '8px 16px' : '10px 13px', width, textAlign: kind === 'check' ? 'center' : 'left',
                    boxShadow: '0 2px 8px rgba(15,23,42,.06)', cursor: tip ? 'help' : 'default' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7, justifyContent: kind === 'check' ? 'center' : 'flex-start' }}>
          {num != null && <span style={{ width: 20, height: 20, borderRadius: '50%', display: 'grid', placeItems: 'center', fontSize: 11, fontWeight: 800,
                                         background: kind === 'start' ? 'rgba(255,255,255,.2)' : C.primary, color: '#fff', flex: '0 0 20px' }}>{num}</span>}
          <span style={{ fontSize: 12.5, fontWeight: 700 }}>{title}</span>
        </div>
        {body && <div style={{ fontSize: 11.5, opacity: 0.85, marginTop: 3, lineHeight: 1.45 }}>{body}</div>}
      </div>
    </Tip>
  )
}

/** Left-to-right chain of nodes. */
export function FlowRow({ nodes }) {
  return (
    <div style={{ display: 'flex', alignItems: 'stretch', gap: 0, overflowX: 'auto', paddingBottom: 4 }}>
      {nodes.map((nd, i) => (
        <Fragment key={i}>
          <div style={{ flex: '1 1 0', minWidth: 130, display: 'flex' }}><div style={{ flex: 1, display: 'flex' }}><FlowNode {...nd} /></div></div>
          {i < nodes.length - 1 && <div style={{ flex: '0 0 28px', display: 'grid', placeItems: 'center', color: C.primary }}><ArrowRight size={18} strokeWidth={2.5} /></div>}
        </Fragment>
      ))}
    </div>
  )
}

/**
 * Top-down decision ladder: each check passes downwards ("yes"); a "no"
 * branches right to its outcome. steps = [{title, body, no, tip}], end = {title, body}.
 */
export function DecisionFlow({ start, steps, end }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 340px) 70px minmax(0, 260px)', alignItems: 'center', rowGap: 0 }}>
      <FlowNode kind="start" {...start} />
      <div /><div />
      {steps.map((s, i) => (
        <Fragment key={i}>
          <div style={{ display: 'grid', placeItems: 'center', color: C.textMuted, height: 26, fontSize: 10.5 }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}><ArrowDown size={14} />{i ? 'yes' : ''}</span>
          </div><div /><div />
          <FlowNode kind="check" title={s.title} body={s.body} tip={s.tip} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 2, color: C.red, fontSize: 10.5, fontWeight: 700, padding: '0 4px' }}>
            <div style={{ flex: 1, height: 1.5, background: '#fca5a5' }} />no<ArrowRight size={13} />
          </div>
          <FlowNode kind="stop" title={s.no} />
        </Fragment>
      ))}
      <div style={{ display: 'grid', placeItems: 'center', color: C.textMuted, height: 26, fontSize: 10.5 }}>
        <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}><ArrowDown size={14} />yes</span>
      </div><div /><div />
      <FlowNode kind="good" {...end} />
    </div>
  )
}
