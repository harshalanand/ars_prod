import { useMemo } from 'react'
import { Search, X, Check } from 'lucide-react'
import { C } from '@/theme/colors'

// Shared faceted column filter for the FA & CONS tables.
//   filter shape: { sel: string[], q: string, mode: 'in' | 'out' }
//     • sel  = ticked distinct values (exact match)  → drives the filter when non-empty
//     • q    = free text; when nothing is ticked it acts as a comma-separated OR
//              "contains" filter; it also filters the value checklist (find-as-you-type)
//     • mode = 'in' (keep matches) | 'out' (exclude matches)

export function passColumn(f, rawVal) {
  if (!f) return true
  const sel = f.sel || []
  const q = (f.q || '').trim().toLowerCase()
  const val = rawVal == null ? '' : String(rawVal)
  let match
  if (sel.length) match = sel.includes(val)
  else if (q) {
    const toks = q.split(',').map(t => t.trim()).filter(Boolean)
    match = toks.length ? toks.some(t => val.toLowerCase().includes(t)) : true
  } else return true
  return f.mode === 'out' ? !match : match
}

export function filterActive(f) {
  return !!(f && ((f.sel && f.sel.length) || (f.q && f.q.trim())))
}

// Global search across an array of a row's cell values — comma = OR.
export function matchGlobal(gq, vals) {
  const toks = (gq || '').toLowerCase().split(',').map(t => t.trim()).filter(Boolean)
  if (!toks.length) return true
  return vals.some(v => {
    const s = String(v ?? '').toLowerCase()
    return toks.some(t => s.includes(t))
  })
}

// Client-side CSV export of the currently-shown rows. `valueFn(col,row)` returns the
// plain cell value; defaults to a formatted/raw read. Opens in Excel (UTF-8 BOM).
export function exportCsv(filename, columns, rows, valueFn) {
  const val = valueFn || ((c, r) => (c.fmt ? c.fmt(r[c.key]) : r[c.key]))
  const esc = v => {
    const s = v == null ? '' : String(v)
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s
  }
  const header = columns.map(c => esc(c.label)).join(',')
  const body = rows.map(r => columns.map(c => esc(val(c, r))).join(',')).join('\n')
  const blob = new Blob(['﻿' + header + '\n' + body], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename.endsWith('.csv') ? filename : filename + '.csv'
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href)
}

const MAX_SHOWN = 500

export default function ColumnFilterPopover({ label, values, value, onChange, onClose, pos }) {
  const f = value || { sel: [], q: '', mode: 'in' }
  const sel = f.sel || []
  const q = f.q || ''

  const shown = useMemo(() => {
    const s = q.trim().toLowerCase()
    const base = values || []
    const list = s ? base.filter(v => String(v).toLowerCase().includes(s)) : base
    return list
  }, [values, q])
  const shownCapped = shown.slice(0, MAX_SHOWN)

  const setF = (patch) => onChange({ sel, q, mode: f.mode || 'in', ...patch })
  const toggle = (v) => setF({ sel: sel.includes(v) ? sel.filter(x => x !== v) : [...sel, v] })
  const selectAllShown = () => setF({ sel: [...new Set([...sel, ...shown])] })
  const clearSel = () => setF({ sel: [] })

  const btn = (on) => ({
    flex: 1, fontSize: 10, fontWeight: 700, padding: '4px 6px', cursor: 'pointer', borderRadius: 5,
    border: `1px solid ${on ? C.primary : C.inputBd}`, background: on ? C.primaryLt : C.card, color: on ? C.primary : C.textMuted,
  })

  return (
    <>
      <div onClick={onClose} style={{ position: 'fixed', inset: 0, zIndex: 65 }} />
      <div style={{ position: 'fixed', left: Math.min(pos.x, window.innerWidth - 270), top: Math.min(pos.y + 4, window.innerHeight - 360),
        zIndex: 66, width: 258, background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 8, boxShadow: '0 10px 30px rgba(0,0,0,.16)', padding: 10 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: C.textSub }}>Filter · {label}</span>
          <button onClick={onClose} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><X size={13} color={C.textMuted} /></button>
        </div>

        <div style={{ display: 'flex', gap: 4, marginBottom: 6 }}>
          {[['in', '= includes'], ['out', '≠ excludes']].map(([m, lbl]) => (
            <button key={m} onClick={() => setF({ mode: m })} style={btn((f.mode || 'in') === m)}>{lbl}</button>
          ))}
        </div>

        <div style={{ position: 'relative', marginBottom: 6 }}>
          <Search size={12} style={{ position: 'absolute', left: 7, top: 7, color: C.textMuted }} />
          <input autoFocus value={q} onChange={e => setF({ q: e.target.value })}
            placeholder="Search values, or a,b,c…"
            style={{ width: '100%', fontSize: 12, padding: '5px 7px 5px 24px', border: `1px solid ${C.inputBd}`, borderRadius: 5, background: C.inputBg, color: C.text, boxSizing: 'border-box' }} />
          {q && <button onClick={() => setF({ q: '' })} style={{ position: 'absolute', right: 5, top: 6, background: 'none', border: 'none', cursor: 'pointer' }}><X size={11} color={C.textMuted} /></button>}
        </div>

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
          <span style={{ fontSize: 10, color: C.textMuted }}>
            {sel.length ? `${sel.length} selected` : `${shown.length} value${shown.length === 1 ? '' : 's'}`}
          </span>
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={selectAllShown} disabled={!shown.length} style={{ fontSize: 10, fontWeight: 700, color: C.primary, background: 'none', border: 'none', cursor: 'pointer' }}>Select shown</button>
            <button onClick={clearSel} disabled={!sel.length} style={{ fontSize: 10, color: C.textMuted, background: 'none', border: 'none', cursor: 'pointer' }}>Clear</button>
          </div>
        </div>

        <div style={{ maxHeight: 190, overflow: 'auto', border: `1px solid ${C.cardBorder}`, borderRadius: 6 }}>
          {shownCapped.length ? shownCapped.map(v => {
            const on = sel.includes(v)
            return (
              <label key={v} title={v} style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '4px 8px', fontSize: 12, cursor: 'pointer', color: C.textSub, borderBottom: `1px solid ${C.headerBg}` }}>
                <span style={{ width: 14, height: 14, borderRadius: 3, border: `1.5px solid ${on ? C.primary : C.inputBd}`, background: on ? C.primary : C.card, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                  {on && <Check size={10} color="#fff" />}
                </span>
                <input type="checkbox" checked={on} onChange={() => toggle(v)} style={{ display: 'none' }} />
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v === '' ? '(blank)' : v}</span>
              </label>
            )
          }) : <div style={{ padding: 10, fontSize: 11, color: C.textMuted, textAlign: 'center' }}>No matching values</div>}
        </div>
        {shown.length > MAX_SHOWN && (
          <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4 }}>Showing first {MAX_SHOWN} — refine the search.</div>
        )}

        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 6, marginTop: 8 }}>
          <button onClick={() => onChange(null)} style={{ fontSize: 11, color: C.textMuted, background: 'none', border: 'none', cursor: 'pointer' }}>Reset column</button>
          <button onClick={onClose} style={{ fontSize: 11, fontWeight: 700, color: C.primary, background: 'none', border: 'none', cursor: 'pointer' }}>Done</button>
        </div>
      </div>
    </>
  )
}
