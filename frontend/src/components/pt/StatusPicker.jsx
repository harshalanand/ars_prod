// Clickable status chip with a dropdown for quick state changes —
// no full-form Edit needed. Used on the detail page header and the list table.
//
// Props:
//   value     — current STATUS string
//   onChange  — async (newStatus) => void; component handles its own busy state
//   disabled  — disable interaction (read-only contexts)
import { useEffect, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'

const STATUS_OPTIONS = [
  { value: 'NOT_STARTED', label: 'Not Started', dot: '#6B7280' },
  { value: 'IN_PROGRESS', label: 'In Progress', dot: '#2563EB' },
  { value: 'BLOCKED',     label: 'Blocked',     dot: '#DC2626' },
  { value: 'ON_HOLD',     label: 'On Hold',     dot: '#9CA3AF' },
  { value: 'COMPLETED',   label: 'Completed',   dot: '#16A34A' },
  { value: 'CANCELLED',   label: 'Cancelled',   dot: '#94A3B8' },
  { value: 'DRAFT',       label: 'Draft',       dot: '#A3A3A3' },
]

const COLOURS = {
  DRAFT:       { bg: '#F3F4F6', fg: '#374151' },
  NOT_STARTED: { bg: '#E5E7EB', fg: '#374151' },
  IN_PROGRESS: { bg: '#DBEAFE', fg: '#1D4ED8' },
  BLOCKED:     { bg: '#FEE2E2', fg: '#B91C1C' },
  ON_HOLD:     { bg: '#F3F4F6', fg: '#6B7280' },
  COMPLETED:   { bg: '#DCFCE7', fg: '#15803D' },
  CANCELLED:   { bg: '#F1F5F9', fg: '#64748B' },
}

export default function StatusPicker({ value, onChange, disabled = false }) {
  const [open, setOpen]   = useState(false)
  const [busy, setBusy]   = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const c = COLOURS[value] || { bg: '#E5E7EB', fg: '#374151' }
  const handlePick = async (next) => {
    setOpen(false)
    if (next === value) return
    setBusy(true)
    try {
      await onChange(next)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div ref={ref} style={{ position: 'relative', display: 'inline-block' }}>
      <button
        type="button"
        disabled={disabled || busy}
        onClick={(e) => { e.stopPropagation(); setOpen(o => !o) }}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 4,
          padding: '2px 8px', borderRadius: 999,
          fontSize: 11, fontWeight: 600,
          background: c.bg, color: c.fg,
          border: 'none',
          cursor: disabled ? 'default' : (busy ? 'wait' : 'pointer'),
          opacity: busy ? 0.6 : 1,
          whiteSpace: 'nowrap',
        }}
      >
        {busy ? '...' : value || '—'}
        {!disabled && <ChevronDown size={12} />}
      </button>

      {open && !disabled && (
        <div
          onClick={(e) => e.stopPropagation()}
          style={{
            position: 'absolute', top: '100%', left: 0, marginTop: 4,
            background: '#fff', border: '1px solid #e5e7eb',
            borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,.12)',
            minWidth: 160, zIndex: 100, padding: 4,
          }}
        >
          {STATUS_OPTIONS.map(opt => (
            <button
              key={opt.value}
              onClick={() => handlePick(opt.value)}
              style={{
                display: 'flex', alignItems: 'center', gap: 8,
                width: '100%', padding: '6px 10px',
                border: 'none', background: opt.value === value ? '#eef2ff' : 'transparent',
                cursor: 'pointer', borderRadius: 4,
                fontSize: 12, color: '#111827', textAlign: 'left',
              }}
              onMouseEnter={(e) => { if (opt.value !== value) e.currentTarget.style.background = '#f9fafb' }}
              onMouseLeave={(e) => { if (opt.value !== value) e.currentTarget.style.background = 'transparent' }}
            >
              <span style={{
                width: 8, height: 8, borderRadius: 999,
                background: opt.dot, flexShrink: 0,
              }} />
              <span>{opt.label}</span>
              {opt.value === value && <span style={{ marginLeft: 'auto', color: '#4f46e5' }}>✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
