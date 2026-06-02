/**
 * AutoContMappingsPage — Auto Cont % (SQL-direct pipeline)
 * Cont_mappings + Cont_mapping_assignments are shared with the pandas
 * pipeline. We render the same CRUD UI with a clarifying banner.
 */
import { Database } from 'lucide-react'
import ContribMappingsPage from './ContribMappingsPage'

export default function AutoContMappingsPage() {
  return (
    <div>
      <div style={{
        padding: '8px 12px', marginBottom: 14, borderRadius: 8,
        background: '#eff6ff', border: '1px solid #bfdbfe', color: '#1e40af',
        fontSize: 11, display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <Database size={14} />
        <span>
          Mappings and assignments are <b>shared</b> with the Contribution % (pandas) pipeline —
          edits here also affect that menu. Both pipelines read from <code>Cont_mappings</code> and <code>Cont_mapping_assignments</code>.
        </span>
      </div>
      <ContribMappingsPage />
    </div>
  )
}
