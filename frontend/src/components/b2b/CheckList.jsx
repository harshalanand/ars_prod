import { useState } from 'react'
import { Check, AlertTriangle, XCircle, Info, ChevronDown, ChevronRight } from 'lucide-react'
import { C } from '@/theme/colors'

export const LEVEL = {
  block: { label: 'Must fix', fg: C.red,     bg: C.redBg,   bd: C.redBd,   Icon: XCircle },
  warn:  { label: 'Warning',  fg: C.amber,   bg: C.amberBg, bd: C.amberBd, Icon: AlertTriangle },
  info:  { label: 'Note',     fg: C.blue,    bg: C.blueBg,  bd: C.blueBd,  Icon: Info },
  ok:    { label: 'OK',       fg: C.green,   bg: C.greenBg, bd: C.greenBd, Icon: Check },
}

const fmt = n => (n == null ? null : Math.round(+n).toLocaleString())

/**
 * items   — [{code, level, title, detail, count, units, sample, sample_label, sheet}]
 * actions — optional { [code]: (item) => ReactNode } rendered on that row, e.g.
 *           a "swap and check again" button for STORE_SWAP.
 */
export default function CheckList({ items = [], actions = {}, empty = 'No checks to show' }) {
  if (!items.length) {
    return <div style={{ fontSize: 11, color: C.textMuted, padding: 8 }}>{empty}</div>
  }
  // minmax(0, 1fr): a grid track otherwise widens to fit each row's one-line
  // detail text and the list spills out of its card.
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 5 }}>
      {items.map((it, i) => <Row key={`${it.code}-${i}`} it={it} action={actions[it.code]} />)}
    </div>
  )
}

function Row({ it, action }) {
  const [open, setOpen] = useState(false)
  const L = LEVEL[it.level] || LEVEL.info
  const hasMore = (it.sample && it.sample.length) || it.detail
  return (
    <div style={{ border: `1px solid ${L.bd}`, background: L.bg, borderRadius: 6, padding: '6px 9px', minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        <L.Icon size={14} style={{ color: L.fg, flex: '0 0 14px', marginTop: 1 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: C.text }}>{it.title}</div>
          {it.detail && !open && (
            <div style={{ fontSize: 10.5, color: C.textSub, marginTop: 1,
                          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {it.detail}
            </div>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: '0 0 auto' }}>
          {it.units != null && (
            <span style={{ fontSize: 10, fontWeight: 700, color: L.fg, fontVariantNumeric: 'tabular-nums' }}>
              {fmt(it.units)} pcs
            </span>
          )}
          {it.sheet && it.sheet !== 'Both' && (
            <span className="badge" style={{ background: C.card, color: C.textSub, border: `1px solid ${C.cardBorder}` }}>
              {it.sheet}
            </span>
          )}
          {hasMore && (
            <button type="button" onClick={() => setOpen(o => !o)} className="btn-secondary btn-sm"
              aria-expanded={open}>
              {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />} Details
            </button>
          )}
        </div>
      </div>
      {open && (
        <div style={{ marginTop: 6, paddingLeft: 22, display: 'grid', gap: 5 }}>
          {it.detail && <div style={{ fontSize: 10.5, color: C.textSub, lineHeight: 1.45 }}>{it.detail}</div>}
          {it.sample && it.sample.length > 0 && (
            <div>
              <div style={{ fontSize: 9.5, fontWeight: 700, color: C.textMuted, textTransform: 'uppercase',
                            letterSpacing: '.05em', marginBottom: 3 }}>
                {it.sample_label ? `${it.sample_label}s` : 'Examples'}
                {it.count != null && it.count > it.sample.length ? ` · first ${it.sample.length} of ${fmt(it.count)}` : ''}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                {it.sample.map((s, j) => (
                  <code key={j} style={{ fontSize: 10, background: C.card, border: `1px solid ${C.cardBorder}`,
                                         borderRadius: 3, padding: '1px 5px', color: C.codeColor }}>{s}</code>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
      {action && <div style={{ marginTop: 6, paddingLeft: 22 }}>{action(it)}</div>}
    </div>
  )
}
