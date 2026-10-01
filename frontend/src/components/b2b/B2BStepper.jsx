import { useNavigate } from 'react-router-dom'
import { Check, AlertTriangle, Loader2 } from 'lucide-react'
import { C } from '@/theme/colors'

// The five steps, in order. Each only makes sense once the one before it has
// finished, so every GRT ALC page shows where it sits in the sequence.
export const STEPS = [
  { step: 1, key: 'upload',   name: 'Upload',    path: '/bin-alloc/upload' },
  { step: 2, key: 'settings', name: 'Settings',  path: '/bin-alloc/settings' },
  { step: 3, key: 'mbq',      name: 'Build MBQ', path: '/bin-alloc/mbq' },
  { step: 4, key: 'run',      name: 'Allocate',  path: '/bin-alloc/run' },
  { step: 5, key: 'sessions', name: 'Review',    path: '/bin-alloc/sessions' },
]

const TONE = {
  done:  { dot: C.green, bg: C.card,      fg: '#fff' },
  warn:  { dot: C.amber, bg: C.card,      fg: '#fff' },
  running: { dot: C.indigo, bg: C.card,   fg: '#fff' },
  now:   { dot: C.primary, bg: C.primaryLt, fg: '#fff' },
  todo:  { dot: C.grayBd, bg: C.card,     fg: C.textSub },
  later: { dot: C.grayBg, bg: C.card,     fg: C.textMuted },
}

/**
 * pipeline — [{step, key, state, note}] from /b2b/overview (optional).
 * current  — the key of the page showing the stepper.
 */
export default function B2BStepper({ pipeline, current }) {
  const nav = useNavigate()
  const by = Object.fromEntries((pipeline || []).map(p => [p.key, p]))
  return (
    <div style={{ display: 'flex', background: C.card, border: `1px solid ${C.cardBorder}`,
                  borderRadius: 8, overflow: 'hidden' }}>
      {STEPS.map((s, i) => {
        const p = by[s.key] || {}
        const state = s.key === current ? 'now' : (p.state || 'todo')
        const t = TONE[state] || TONE.todo
        return (
          <button key={s.key} type="button" onClick={() => nav(s.path)}
            title={p.note || s.name}
            style={{ flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 8,
                     padding: '7px 10px', background: t.bg, border: 'none', cursor: 'pointer',
                     borderRight: i < STEPS.length - 1 ? `1px solid ${C.cardBorder}` : 'none',
                     textAlign: 'left' }}>
            <span style={{ width: 18, height: 18, borderRadius: '50%', flex: '0 0 18px',
                           display: 'grid', placeItems: 'center', background: t.dot, color: t.fg,
                           fontSize: 10, fontWeight: 800 }}>
              {state === 'done' ? <Check size={11} strokeWidth={3} />
                : state === 'warn' ? <AlertTriangle size={10} strokeWidth={3} />
                : state === 'running' ? <Loader2 size={10} strokeWidth={3} className="animate-spin" /> : s.step}
            </span>
            <span style={{ minWidth: 0 }}>
              <span style={{ display: 'block', fontSize: 11, fontWeight: 700,
                             color: state === 'later' ? C.textMuted : C.text }}>{s.name}</span>
              <span style={{ display: 'block', fontSize: 9.5, color: C.textMuted, whiteSpace: 'nowrap',
                             overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {s.key === current && !p.note ? 'you are here' : (p.note || '—')}
              </span>
            </span>
          </button>
        )
      })}
    </div>
  )
}
