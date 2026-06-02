/**
 * AutoContPlaceholder — shared "coming soon" card for the 5 Auto Cont % pages.
 * Remove (or repurpose) when each page gets its real implementation.
 */
import { Construction } from 'lucide-react'
import { C } from '@/theme/colors'

export default function AutoContPlaceholder({ icon: Icon, title, blurb }) {
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
        textAlign: 'center',
        maxWidth: 640,
      }}>
        <div style={{
          width: 56, height: 56, borderRadius: '50%',
          background: C.primaryLight,
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          marginBottom: 14,
        }}>
          <Construction size={28} color={C.primary} />
        </div>
        <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 8 }}>
          Coming Soon — SQL-direct pipeline
        </div>
        <div style={{ fontSize: 13, color: C.textMuted, lineHeight: 1.55, maxWidth: 480, margin: '0 auto' }}>
          {blurb}
        </div>
        <div style={{
          marginTop: 18, padding: '8px 12px', borderRadius: 8,
          background: '#fef3c7', color: '#92400e',
          fontSize: 11, fontWeight: 600, display: 'inline-block',
        }}>
          Superadmin preview · not wired to backend yet
        </div>
      </div>
    </div>
  )
}
