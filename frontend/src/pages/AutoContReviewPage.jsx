/**
 * AutoContReviewPage — Auto Cont % (SQL-direct pipeline)
 * Browse AutoCont_FINAL_* tables, preview, download as CSV, drop.
 */
import { useState, useEffect } from 'react'
import { autoContAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  ClipboardCheck, RefreshCw, Download, Trash2, Database, Loader2,
} from 'lucide-react'
import { C } from '@/theme/colors'

export default function AutoContReviewPage() {
  const [tables, setTables] = useState([])
  const [selected, setSelected] = useState(null)
  const [preview, setPreview] = useState(null)
  const [loadingTables, setLoadingTables] = useState(false)
  const [loadingPreview, setLoadingPreview] = useState(false)
  const [downloading, setDownloading] = useState(false)

  const loadTables = async () => {
    setLoadingTables(true)
    try {
      const { data } = await autoContAPI.listTables()
      setTables(data?.data?.tables || [])
    } catch { toast.error('Failed to load tables') }
    finally { setLoadingTables(false) }
  }

  const loadPreview = async (name) => {
    setSelected(name)
    setPreview(null)
    setLoadingPreview(true)
    try {
      const { data } = await autoContAPI.preview(name, 200)
      setPreview(data?.data || null)
    } catch { toast.error('Preview failed') }
    finally { setLoadingPreview(false) }
  }

  const drop = async (name) => {
    if (!confirm(`Drop ${name}?\n\nThis cannot be undone.`)) return
    try {
      await autoContAPI.dropTable(name)
      toast.success('Dropped')
      if (selected === name) { setSelected(null); setPreview(null) }
      loadTables()
    } catch { toast.error('Drop failed') }
  }

  const download = async (name) => {
    setDownloading(true)
    try {
      const resp = await autoContAPI.downloadTable(name)
      const blob = new Blob([resp.data], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${name}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } catch { toast.error('Download failed') }
    finally { setDownloading(false) }
  }

  useEffect(() => { loadTables() }, [])

  return (
    <div style={{ color: C.text }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h1 style={{ fontSize: 20, fontWeight: 800, margin: 0, display: 'flex', alignItems: 'center', gap: 10 }}>
          <ClipboardCheck size={20} color={C.primary} /> Auto Cont % — Review
        </h1>
        <button onClick={loadTables} style={{
          padding: '6px 14px', borderRadius: 6, fontSize: 12, fontWeight: 600,
          border: `1px solid ${C.cardBorder}`, background: '#fff', color: C.textSub,
          cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
        }}>
          <RefreshCw size={12} className={loadingTables ? 'spin' : ''} /> Refresh
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '340px 1fr', gap: 16 }}>
        {/* ───────── Left: table list ───────── */}
        <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
          <div style={{ padding: '12px 16px', background: C.headerBg, borderBottom: `1px solid ${C.cardBorder}`, fontSize: 13, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Database size={14} /> Output tables ({tables.length})
          </div>
          <div style={{ maxHeight: 560, overflowY: 'auto' }}>
            {tables.length === 0 && (
              <div style={{ padding: 24, textAlign: 'center', color: C.textMuted, fontSize: 12 }}>
                No <code>AutoCont_FINAL_*</code> tables yet. Run a job from <b>Execute</b>.
              </div>
            )}
            {tables.map(t => (
              <div key={t}
                   onClick={() => loadPreview(t)}
                   style={{
                     padding: '10px 12px', borderBottom: `1px solid ${C.cardBorder}`,
                     cursor: 'pointer', fontSize: 11, fontFamily: 'monospace',
                     background: selected === t ? C.primaryLight : '#fff',
                     color: selected === t ? C.primary : C.text,
                     display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6,
                   }}>
                <div style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {t}
                </div>
                <button onClick={e => { e.stopPropagation(); drop(t) }}
                        title="Drop table"
                        style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 4, color: C.red, flexShrink: 0 }}>
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* ───────── Right: preview ───────── */}
        <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
          {!selected && (
            <div style={{ padding: 40, textAlign: 'center', color: C.textMuted, fontSize: 13 }}>
              Pick a table on the left to preview the first 200 rows.
            </div>
          )}
          {selected && (
            <>
              <div style={{ padding: '10px 16px', background: C.headerBg, borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 700, fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{selected}</div>
                  {preview && (
                    <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>
                      {preview.total_rows?.toLocaleString()} rows · {preview.columns?.length} columns · showing first {preview.preview?.length || 0}
                    </div>
                  )}
                </div>
                <button onClick={() => download(selected)} disabled={downloading} style={{
                  padding: '6px 14px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                  border: 'none', background: C.primary, color: '#fff',
                  cursor: downloading ? 'wait' : 'pointer',
                  display: 'flex', alignItems: 'center', gap: 6,
                }}>
                  {downloading ? <Loader2 size={12} className="spin" /> : <Download size={12} />}
                  Download CSV
                </button>
              </div>

              <div style={{ overflow: 'auto', maxHeight: 580 }}>
                {loadingPreview && (
                  <div style={{ padding: 40, textAlign: 'center', color: C.textMuted, fontSize: 13, display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 8 }}>
                    <Loader2 size={14} className="spin" /> Loading preview…
                  </div>
                )}
                {preview && !loadingPreview && (
                  <table style={{ borderCollapse: 'collapse', fontSize: 11, fontFamily: 'monospace', minWidth: '100%' }}>
                    <thead style={{ position: 'sticky', top: 0, background: '#f1f5f9', zIndex: 1 }}>
                      <tr>
                        {preview.columns.map(c => (
                          <th key={c} style={{
                            padding: '6px 10px', borderBottom: `1px solid ${C.cardBorder}`,
                            borderRight: `1px solid ${C.cardBorder}`,
                            textAlign: 'left', fontWeight: 700, fontSize: 10,
                            color: C.textSub, whiteSpace: 'nowrap',
                          }}>{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {preview.preview.map((row, i) => (
                        <tr key={i} style={{ borderBottom: `1px solid #f1f5f9` }}>
                          {preview.columns.map(c => (
                            <td key={c} style={{
                              padding: '5px 10px', borderRight: `1px solid #f1f5f9`,
                              whiteSpace: 'nowrap',
                              color: row[c] == null ? C.textMuted : C.text,
                            }}>
                              {row[c] == null ? '—' : String(row[c])}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}
