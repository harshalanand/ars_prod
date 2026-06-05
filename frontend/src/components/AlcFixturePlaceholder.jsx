/**
 * AlcFixturePlaceholder — shared "coming soon" card for the 5 ALC_Fixture pages.
 * Each page renders the MSA-STK Allocation Engine blueprint stage it owns.
 * Remove (or repurpose) when each page gets its real implementation.
 */
import { Construction } from 'lucide-react'
import { C } from '@/theme/colors'

export default function AlcFixturePlaceholder({ icon: Icon, title, blurb, stages }) {
  return (
    <div style={{ color: C.text }}>
      <h1 style={{ fontSize: 20, fontWeight: 800, margin: '0 0 20px', display: 'flex', alignItems: 'center', gap: 10 }}>
        <Icon size={20} color={C.primary} /> {title}
      </h1>

      <div style={{
        background: C.cardBg,
        border: `1px solid ${C.cardBorder}`,
        borderRadius: 12,
        padding: 32,
        maxWidth: 760,
      }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{
            width: 56, height: 56, borderRadius: '50%',
            background: C.primaryLight,
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            marginBottom: 14,
          }}>
            <Construction size={28} color={C.primary} />
          </div>
          <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 8 }}>
            Coming Soon — MSA-STK Allocation Engine
          </div>
          <div style={{ fontSize: 13, color: C.textMuted, lineHeight: 1.55, maxWidth: 580, margin: '0 auto' }}>
            {blurb}
          </div>
        </div>

        {stages && stages.length > 0 && (
          <div style={{ marginTop: 20, borderTop: `1px solid ${C.cardBorder}`, paddingTop: 16 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 10 }}>
              Blueprint stages handled here
            </div>
            <ul style={{ margin: 0, padding: '0 0 0 18px', fontSize: 12.5, color: C.text, lineHeight: 1.7 }}>
              {stages.map((s, i) => (
                <li key={i}><strong>{s.tag}</strong> — {s.text}</li>
              ))}
            </ul>
          </div>
        )}

        <div style={{ marginTop: 18, textAlign: 'center' }}>
          <div style={{
            padding: '8px 12px', borderRadius: 8,
            background: '#fef3c7', color: '#92400e',
            fontSize: 11, fontWeight: 600, display: 'inline-block',
          }}>
            Superadmin preview · v1.0 blueprint (2026-05-21) · not wired to backend yet
          </div>
        </div>
      </div>
    </div>
  )
}
