import { useEffect, useState } from 'react'
import { faBusy } from '@/services/api'

/**
 * Global progress indicator for the whole FA & CONS module. Subscribes to the
 * in-flight /fa-cons/ request counter (api.js) and shows a CENTERED "delivery
 * truck" loader over a transparent scrim whenever ANY FA & CONS page/process
 * runs — load, Calculate, upload, allocation run, gap build, save, export.
 * A clear "wait for it to finish" signal so users don't act mid-task.
 */
export default function FaConsBusyBar() {
  const [s, setS] = useState({ count: 0, label: '' })
  useEffect(() => faBusy.subscribe(setS), [])
  if (s.count <= 0) return null
  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 300,
      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 14,
      background: 'rgba(255,255,255,0.35)', backdropFilter: 'blur(1.5px)', WebkitBackdropFilter: 'blur(1.5px)',
    }}>
      <style>{`
        @keyframes faTruckDrive { 0%,100%{transform:translate(-3px,0)} 50%{transform:translate(3px,-2px)} }
        @keyframes faTruckWheel { to { transform: rotate(360deg) } }
        @keyframes faTruckRoad  { to { background-position: -22px 0 } }
      `}</style>

      <div style={{ position: 'relative', width: 132, height: 68 }}>
        <svg viewBox="0 0 92 54" width="120" height="70"
          style={{ position: 'absolute', left: '50%', top: 0, transform: 'translateX(-50%)', animation: 'faTruckDrive 1.5s ease-in-out infinite' }}>
          {/* cargo body */}
          <rect x="4" y="12" width="46" height="24" rx="3" fill="#6366f1" />
          {/* cab */}
          <path d="M50 18 h15 l11 11 v7 h-26 z" fill="#818cf8" />
          {/* window */}
          <rect x="54" y="21" width="9" height="7" rx="1.5" fill="#e0e7ff" />
          {/* headlight */}
          <rect x="74.5" y="31" width="2.5" height="4" rx="1" fill="#fbbf24" />
          {/* wheels (spinning) */}
          <g style={{ transformOrigin: '18px 40px', animation: 'faTruckWheel 0.6s linear infinite' }}>
            <circle cx="18" cy="40" r="6.5" fill="#1f2937" />
            <circle cx="18" cy="40" r="2" fill="#e5e7eb" />
            <line x1="18" y1="34.5" x2="18" y2="45.5" stroke="#e5e7eb" strokeWidth="1.4" />
          </g>
          <g style={{ transformOrigin: '58px 40px', animation: 'faTruckWheel 0.6s linear infinite' }}>
            <circle cx="58" cy="40" r="6.5" fill="#1f2937" />
            <circle cx="58" cy="40" r="2" fill="#e5e7eb" />
            <line x1="58" y1="34.5" x2="58" y2="45.5" stroke="#e5e7eb" strokeWidth="1.4" />
          </g>
        </svg>
        {/* moving road */}
        <div style={{
          position: 'absolute', bottom: 4, left: 0, right: 0, height: 3, borderRadius: 3,
          background: 'repeating-linear-gradient(90deg, rgba(99,102,241,.55) 0 11px, transparent 11px 22px)',
          animation: 'faTruckRoad 0.4s linear infinite',
        }} />
      </div>

      <div role="status" aria-live="polite" style={{
        fontSize: 13, fontWeight: 700, color: '#1f2937', textShadow: '0 1px 2px rgba(255,255,255,.9)',
      }}>
        {s.label || 'Working…'}{s.count > 1 && <span style={{ opacity: .55, fontWeight: 400 }}> · {s.count}</span>}
      </div>
    </div>
  )
}
