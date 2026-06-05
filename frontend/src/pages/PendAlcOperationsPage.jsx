/**
 * PendAlcOperationsPage — soft-revert audit log for BDC / DO / Manual / Approve ops.
 *
 * Each row in ARS_PEND_ALC_OPERATIONS is one write event with a JSON payload
 * describing the deltas. "Revert" replays the deltas in reverse and stamps
 * REVERTED_AT on the operation row (record stays, audit preserved).
 */
import { useState, useEffect, useCallback, useMemo } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  History, RefreshCw, Undo2, AlertTriangle, CheckCircle2, X, Filter, Search,
  Database,
} from 'lucide-react'

const C = {
  primary: '#4f46e5', green: '#16a34a', amber: '#d97706', red: '#dc2626',
  text: '#1e293b', textSub: '#64748b', textMuted: '#94a3b8',
  border: '#e2e8f0', bg: '#f8fafc', card: '#ffffff',
}

const OP_TYPE_COLOR = { BDC: C.primary, DO: C.amber, MANUAL: '#0891b2', APPROVE: C.green, ADHOC_CLOSE: C.red }

const fmt = (n) => Number.isFinite(+n) ? Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 }) : '—'
const fmtDt = (s) => s ? new Date(s).toLocaleString() : '—'

export default function PendAlcOperationsPage() {
  const [rows, setRows]       = useState([])
  const [loading, setLoading] = useState(false)
  const [filterType, setFilterType] = useState('')   // '' | 'BDC' | 'DO' | 'MANUAL' | 'APPROVE' | 'ADHOC_CLOSE'
  const [showReverted, setShowReverted] = useState(true)
  const [search, setSearch]   = useState('')

  // Modal state for revert preview/confirm
  const [modalOp, setModalOp] = useState(null)
  const [preview, setPreview] = useState(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [reverting, setReverting] = useState(false)
  const [revertNote, setRevertNote] = useState('')
  // Async revert tracking — surfaces progress + a completion banner.
  const [revertJobStatus, setRevertJobStatus] = useState(null)
  const [revertJobResult, setRevertJobResult] = useState(null)

  // Backfill state — for old BDCs generated before logging existed
  const [backfilling, setBackfilling] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = { include_reverted: showReverted, limit: 500 }
      if (filterType) params.op_type = filterType
      const { data } = await pendAlcAPI.operationsList(params)
      setRows(data?.data || [])
    } catch {
      toast.error('Failed to load operations')
    } finally { setLoading(false) }
  }, [filterType, showReverted])

  useEffect(() => { load() }, [load])

  const filtered = useMemo(() => {
    if (!search) return rows
    const q = search.toLowerCase()
    return rows.filter(r =>
      (r.op_key || '').toLowerCase().includes(q) ||
      (r.summary || '').toLowerCase().includes(q) ||
      (r.created_by || '').toLowerCase().includes(q)
    )
  }, [rows, search])

  const openRevertModal = async (op) => {
    setModalOp(op); setPreview(null); setRevertNote(''); setPreviewLoading(true)
    try {
      const { data } = await pendAlcAPI.operationsPreview(op.op_id)
      setPreview(data)
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Preview failed')
      setModalOp(null)
    } finally { setPreviewLoading(false) }
  }

  const confirmRevert = async () => {
    if (!modalOp) return
    setReverting(true)
    setRevertJobStatus(null); setRevertJobResult(null)
    try {
      const startResp = await pendAlcAPI.operationsRevertAsync(modalOp.op_id, revertNote)
      const jobId = startResp.data?.job_id
      if (!jobId) throw new Error('No job_id from server')
      setRevertJobStatus({ status: 'pending', progress: 'queued' })

      const finalJob = await new Promise((resolve, reject) => {
        const timer = setInterval(async () => {
          try {
            const s = await pendAlcAPI.asyncJobStatus(jobId)
            const j = s.data?.data
            if (!j) return
            setRevertJobStatus(j)
            if (j.status === 'completed') { clearInterval(timer); resolve(j) }
            else if (j.status === 'failed') {
              clearInterval(timer); reject(new Error(j.error || 'job failed'))
            }
          } catch (err) { clearInterval(timer); reject(err) }
        }, 2000)
      })

      setRevertJobResult(finalJob.result || {})
      toast.success(`Reverted op #${modalOp.op_id}`)
      load()
    } catch (e) {
      toast.error(e.response?.data?.detail || e.message || 'Revert failed')
      setRevertJobStatus(null)
    } finally { setReverting(false) }
  }

  // One-shot: scan ARS_BDC_HISTORY for BDCs generated before logging existed
  // and create op rows so they become revertable from this page.
  const handleBackfillBdc = async () => {
    setBackfilling(true)
    try {
      const { data: prev } = await pendAlcAPI.operationsBackfillBdc(false)
      if (!prev.found) {
        toast('No historical BDCs need backfill', { icon: 'ℹ️' })
        return
      }
      if (!confirm(
        `${prev.found} historical BDC(s) found without an operations entry.\n\n` +
        `Backfill will create one revertable operation per allocation_number.\n\n` +
        `Apply now?`
      )) return
      const { data: app } = await pendAlcAPI.operationsBackfillBdc(true)
      toast.success(`Backfilled ${app.ops_created} BDC operation(s)`)
      load()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Backfill failed')
    } finally { setBackfilling(false) }
  }

  return (
    <div style={{ padding: '16px 20px', fontFamily: 'Inter,system-ui,sans-serif',
                  fontSize: 11, color: C.text, background: C.bg, minHeight: '100vh' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <History size={16} color={C.primary}/>
        <div>
          <div style={{ fontSize: 13, fontWeight: 800 }}>Operations Log + Undo</div>
          <div style={{ fontSize: 10, color: C.textMuted }}>
            BDC, DO, Manual upload, Approve, Adhoc Close — each can be reverted (soft-revert preserves audit)
          </div>
        </div>
        <div style={{ flex: 1 }}/>
        <button onClick={handleBackfillBdc} disabled={backfilling}
          title="Create operation rows for BDCs generated before the operations-log feature existed"
          style={btn(C.border, '#fff', C.textSub)}>
          <Database size={11} style={{ animation: backfilling ? 'spin 1s linear infinite' : 'none' }}/>
          {backfilling ? 'Backfilling…' : 'Backfill historical BDCs'}
        </button>
        <button onClick={load} disabled={loading} style={btn(C.border, '#fff', C.textSub)}>
          <RefreshCw size={11} style={{ animation: loading ? 'spin 1s linear infinite' : 'none' }}/>
          Refresh
        </button>
      </div>

      {/* Toolbar */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    padding: 10, marginBottom: 10, display: 'flex', gap: 8, alignItems: 'center',
                    flexWrap: 'wrap' }}>
        <Filter size={12} color={C.textMuted}/>
        <span style={{ fontSize: 10, fontWeight: 600, color: C.textSub }}>Type:</span>
        {[
          { value: '',             label: 'All' },
          { value: 'BDC',          label: 'BDC' },
          { value: 'DO',           label: 'DO' },
          { value: 'MANUAL',       label: 'MANUAL' },
          { value: 'APPROVE',      label: 'APPROVE' },
          { value: 'ADHOC_CLOSE',  label: 'ADHOC CLOSE' },
        ].map(({ value: t, label }) => (
          <button key={t || 'all'} onClick={() => setFilterType(t)}
            style={{
              fontSize: 10, padding: '4px 10px', borderRadius: 4, cursor: 'pointer',
              border: `1px solid ${filterType === t ? C.primary : C.border}`,
              background: filterType === t ? C.primary : '#fff',
              color: filterType === t ? '#fff' : C.textSub, fontWeight: 600,
            }}>
            {label}
          </button>
        ))}
        <div style={{ width: 1, height: 18, background: C.border, margin: '0 4px' }}/>
        <label style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10, color: C.textSub }}>
          <input type="checkbox" checked={showReverted}
            onChange={e => setShowReverted(e.target.checked)}/>
          Show reverted
        </label>
        <div style={{ flex: 1 }}/>
        <div style={{ position: 'relative' }}>
          <Search size={12} style={{ position: 'absolute', left: 8, top: 7, color: C.textMuted }}/>
          <input value={search} onChange={e => setSearch(e.target.value)}
            placeholder="Search key/summary/user…"
            style={{ fontSize: 11, padding: '5px 10px 5px 26px', borderRadius: 4,
                     border: `1px solid ${C.border}`, width: 240, outline: 'none' }}/>
        </div>
      </div>

      {/* Operations table */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    overflow: 'hidden' }}>
        {loading ? (
          <div style={{ padding: 30, textAlign: 'center', color: C.textMuted }}>Loading…</div>
        ) : filtered.length === 0 ? (
          <div style={{ padding: 30, textAlign: 'center', color: C.textMuted }}>
            No operations match
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead>
              <tr style={{ background: C.bg }}>
                <th style={th()}>OP_ID</th>
                <th style={th()}>TYPE</th>
                <th style={th()}>KEY</th>
                <th style={th()}>DATE</th>
                <th style={th()}>BY</th>
                <th style={th()}>SUMMARY</th>
                <th style={{ ...th(), textAlign: 'right' }}>ROWS</th>
                <th style={{ ...th(), textAlign: 'right' }}>QTY</th>
                <th style={th()}>STATUS</th>
                <th style={{ ...th(), textAlign: 'center', width: 90 }}/>
              </tr>
            </thead>
            <tbody>
              {filtered.map(r => {
                const isReverted = !!r.reverted_at
                return (
                  <tr key={r.op_id}
                    style={{ borderBottom: `1px solid ${C.border}`,
                             background: isReverted ? '#fef2f2' : '#fff',
                             opacity: isReverted ? 0.7 : 1 }}>
                    <td style={td()}><code>#{r.op_id}</code></td>
                    <td style={td()}>
                      <span title={r.op_type}
                            style={{ fontSize: 9, fontWeight: 700, padding: '2px 7px',
                                     borderRadius: 3, whiteSpace: 'nowrap',
                                     background: (OP_TYPE_COLOR[r.op_type] || C.textSub) + '22',
                                     color: OP_TYPE_COLOR[r.op_type] || C.textSub }}>
                        {r.op_type === 'ADHOC_CLOSE' ? 'ADHOC' : r.op_type}
                      </span>
                    </td>
                    <td style={{ ...td(), fontFamily: 'monospace' }}>{r.op_key}</td>
                    <td style={td()}>{fmtDt(r.op_date)}</td>
                    <td style={td()}>{r.created_by || '—'}</td>
                    <td style={{ ...td(), color: C.textSub }}>{r.summary}</td>
                    <td style={{ ...td(), textAlign: 'right', fontWeight: 600 }}>{fmt(r.rows_affected)}</td>
                    <td style={{ ...td(), textAlign: 'right', fontWeight: 600 }}>{fmt(r.qty_total)}</td>
                    <td style={td()}>
                      {isReverted ? (
                        <span style={{ fontSize: 9, fontWeight: 700, padding: '2px 7px', borderRadius: 3,
                                       background: '#fee2e2', color: C.red }}>REVERTED</span>
                      ) : (
                        <span style={{ fontSize: 9, fontWeight: 700, padding: '2px 7px', borderRadius: 3,
                                       background: '#dcfce7', color: C.green }}>ACTIVE</span>
                      )}
                    </td>
                    <td style={{ ...td(), textAlign: 'center' }}>
                      {!isReverted && (
                        <button onClick={() => openRevertModal(r)}
                          style={btn(C.amber, C.amber + '15', C.amber)}>
                          <Undo2 size={10}/> Revert
                        </button>
                      )}
                      {isReverted && r.reverted_by && (
                        <span style={{ fontSize: 9, color: C.textMuted }}
                          title={`Reverted by ${r.reverted_by} at ${fmtDt(r.reverted_at)}`}>
                          by {r.reverted_by}
                        </span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div style={{ marginTop: 8, fontSize: 10, color: C.textMuted }}>
        {filtered.length} of {rows.length} operation{rows.length !== 1 ? 's' : ''}
      </div>

      {/* Revert preview/confirm modal */}
      {modalOp && (
        <div onClick={() => !reverting && setModalOp(null)}
          style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.5)',
                   display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}>
          <div onClick={e => e.stopPropagation()}
            style={{ background: '#fff', borderRadius: 8, width: 560, maxWidth: '92vw',
                     maxHeight: '85vh', overflowY: 'auto', boxShadow: '0 12px 32px rgba(0,0,0,.2)' }}>

            {/* Header */}
            <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.border}`,
                          display: 'flex', alignItems: 'center', gap: 10 }}>
              <Undo2 size={14} color={C.amber}/>
              <div style={{ fontSize: 13, fontWeight: 800 }}>
                Revert {modalOp.op_type} #{modalOp.op_id}
              </div>
              <div style={{ flex: 1 }}/>
              <button onClick={() => setModalOp(null)} disabled={reverting}
                style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 16,
                         color: C.textMuted }}>×</button>
            </div>

            <div style={{ padding: 16 }}>
              {/* Op summary */}
              <div style={{ marginBottom: 14, fontSize: 12 }}>
                <div style={{ color: C.textSub }}>
                  <b>Key:</b> <code>{modalOp.op_key}</code>
                </div>
                <div style={{ color: C.textSub }}>
                  <b>Summary:</b> {modalOp.summary}
                </div>
                <div style={{ color: C.textSub }}>
                  <b>By:</b> {modalOp.created_by || '—'} at {fmtDt(modalOp.op_date)}
                </div>
              </div>

              {/* Preview result */}
              {previewLoading ? (
                <div style={{ padding: 14, textAlign: 'center', color: C.textMuted }}>
                  Checking safety…
                </div>
              ) : preview?.blockers?.length > 0 ? (
                <div style={{ padding: 12, borderRadius: 4,
                              background: C.red + '12', border: `1px solid ${C.red}40` }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6,
                                fontSize: 11, fontWeight: 700, color: C.red, marginBottom: 6 }}>
                    <AlertTriangle size={12}/> Cannot revert — blockers found
                  </div>
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: C.text }}>
                    {preview.blockers.map((b, i) => <li key={i}>{b}</li>)}
                  </ul>
                </div>
              ) : preview ? (
                <div style={{ padding: 12, borderRadius: 4,
                              background: C.green + '12', border: `1px solid ${C.green}40` }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6,
                                fontSize: 11, fontWeight: 700, color: C.green, marginBottom: 6 }}>
                    <CheckCircle2 size={12}/> Safe to revert
                  </div>
                  <div style={{ fontSize: 11, color: C.text }}>
                    Will undo: <b>{fmt(preview.rows_affected)}</b> row{preview.rows_affected !== 1 ? 's' : ''} affected,
                    {' '}<b>{fmt(preview.qty_total)}</b> units total
                  </div>
                </div>
              ) : null}

              {/* Note */}
              {preview?.can_revert && (
                <div style={{ marginTop: 14 }}>
                  <label style={{ fontSize: 10, fontWeight: 700, color: C.textSub,
                                  letterSpacing: '.05em', display: 'block', marginBottom: 4 }}>
                    REVERT NOTE (optional)
                  </label>
                  <input value={revertNote} onChange={e => setRevertNote(e.target.value)}
                    placeholder="Why are you reverting this?"
                    style={{ width: '100%', fontSize: 11, padding: '6px 10px',
                             borderRadius: 4, border: `1px solid ${C.border}`,
                             outline: 'none', boxSizing: 'border-box' }}/>
                </div>
              )}

              {/* Live status of the async revert job */}
              {reverting && revertJobStatus && revertJobStatus.status !== 'completed' && (
                <div style={{ marginTop: 12, padding: '8px 10px', borderRadius: 4,
                              background: '#FFF7ED', border: `1px solid ${C.amber}`,
                              fontSize: 11, color: C.text }}>
                  <div style={{ fontWeight: 700 }}>Revert running — {revertJobStatus.status}</div>
                  <div style={{ color: C.textSub, fontSize: 10 }}>{revertJobStatus.progress || '…'}</div>
                </div>
              )}

              {/* Completion banner */}
              {revertJobResult && (
                <div style={{ marginTop: 12, padding: '10px 12px', borderRadius: 4,
                              background: '#ECFDF5', border: '1px solid #10b981',
                              fontSize: 11, color: C.text }}>
                  <div style={{ fontWeight: 800, color: '#047857', marginBottom: 4 }}>
                    ✓ Revert complete
                  </div>
                  {Object.entries(revertJobResult).map(([k, v]) => (
                    <div key={k} style={{ fontSize: 10 }}>
                      {k}: <b>{typeof v === 'number' ? v.toLocaleString() : String(v)}</b>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div style={{ padding: '10px 16px', borderTop: `1px solid ${C.border}`,
                          display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={() => { setModalOp(null); setRevertJobResult(null); setRevertJobStatus(null) }}
                disabled={reverting}
                style={btn(C.border, '#fff', C.textSub)}>
                {revertJobResult ? 'Close' : 'Cancel'}
              </button>
              <button onClick={confirmRevert}
                disabled={reverting || !preview?.can_revert || !!revertJobResult}
                style={btn(C.amber, C.amber, '#fff')}>
                <Undo2 size={11}
                  style={{ animation: reverting ? 'spin 1s linear infinite' : 'none' }}/>
                {reverting
                  ? (revertJobStatus?.progress ? `${revertJobStatus.progress}…` : 'Reverting…')
                  : (revertJobResult ? 'Done' : 'Confirm Revert')}
              </button>
            </div>
          </div>
        </div>
      )}

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}

const th = () => ({
  padding: '8px 10px', textAlign: 'left', fontSize: 9, fontWeight: 700,
  color: C.textSub, letterSpacing: '.05em', borderBottom: `1px solid ${C.border}`,
})
const td = () => ({ padding: '6px 10px' })
const btn = (border, bg, color) => ({
  fontSize: 10, fontWeight: 700, padding: '5px 12px', borderRadius: 4,
  border: `1px solid ${border}`, background: bg, color: color,
  cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 5,
})
