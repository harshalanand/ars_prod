/**
 * AutoContPresetsPage — Auto Cont % (SQL-direct pipeline)
 * Cont_presets is shared between the pandas pipeline and the SQL pipeline,
 * so we render the same CRUD UI as ContribPresetsPage and add a banner
 * making the sharing explicit.
 */
import { Database } from 'lucide-react'
import { C } from '@/theme/colors'
import ContribPresetsPage from './ContribPresetsPage'

export default function AutoContPresetsPage() {
  return (
    <div>
      <div style={{
        padding: '8px 12px', marginBottom: 14, borderRadius: 8,
        background: '#eff6ff', border: '1px solid #bfdbfe', color: '#1e40af',
        fontSize: 11, display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <Database size={14} />
        <span>
          Presets are <b>shared</b> with the Contribution % (pandas) pipeline —
          edits here also affect that menu. Both pipelines read from <code>Cont_presets</code>.
        </span>
      </div>
      <ContribPresetsPage />
    </div>
  )
}
