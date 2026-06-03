/**
 * PendingDeliveryOrderPage — Daily DO qty entry tool
 * Upload a CSV or enter rows manually to record DO quantities issued by SAP.
 * Deducts from ARS_PEND_ALC and closes hold tracking rows.
 */
import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Upload, Plus, Trash2, Send, RefreshCw, CheckCircle, Truck, Download,
  Database, X, FileText,
} from 'lucide-react'

// Above this row count, switch to bulk mode (no editable table render).
const BULK_THRESHOLD = 100
// Submit in chunks so the user sees progress and the request body stays
// small. 10,000-row chunks match the Manual upload page so DO entry runs
// at the same speed — fast_executemany on the backend keeps each chunk
// sub-second, and the request body stays under axios's 10-min ceiling on
// a cold connection.
const SUBMIT_CHUNK = 10000

// Generate a per-upload session id so chunks roll up to ONE ops_log row
// (revert covers the whole upload, not just chunk 1).
const newSessionId = () =>
  `DO-${new Date().toISOString().slice(0,10).replace(/-/g,'')}-` +
  Math.random().toString(16).slice(2, 8)

const C = {
  primary: '#4f46e5', blue: '#0891b2', green: '#16a34a',
  amber: '#d97706', red: '#dc2626', text: '#1e293b',
  textSub: '#64748b', textMuted: '#94a3b8', border: '#e2e8f0',
  bg: '#f8fafc', card: '#ffffff',
}

const EMPTY_ROW = {
  rdc: '', st_cd: '', article_number: '',
  do_qty: '', do_number: '', allocation_number: '',
}

function parseCsv(text) {
  const lines = text.trim().split('\n')
  if (lines.length < 2) return []
  const hdr = lines[0].split(',').map(h => h.trim().toLowerCase())
  const rdcIdx   = hdr.findIndex(h => ['rdc','source warehouse','source wh','warehouse','wh','vendor'].includes(h))
  const stIdx    = hdr.findIndex(h => ['st_cd','st code','store','store code','dest store','destination store','receiving store','werks'].includes(h))
  const artIdx   = hdr.findIndex(h => ['article_number','material no','material','matnr'].includes(h))
  const qtyIdx   = hdr.findIndex(h => ['do_qty','do qty','qty','quantity'].includes(h))
  const doIdx    = hdr.findIndex(h => ['do_number','do number','delivery order','do no'].includes(h))
  const allocIdx = hdr.findIndex(h => ['allocation_number','allocation number','allocation no','alloc_no','alloc no','alloc number'].includes(h))
  if (rdcIdx < 0 || artIdx < 0 || qtyIdx < 0) return null
  return lines.slice(1).map(l => {
    const cols = l.split(',')
    return {
      rdc:               (cols[rdcIdx]  || '').trim(),
      st_cd:             stIdx >= 0    ? (cols[stIdx]    || '').trim() : '',
      article_number:    (cols[artIdx]  || '').trim(),
      do_qty:            (cols[qtyIdx]  || '').trim(),
      do_number:         doIdx >= 0    ? (cols[doIdx]    || '').trim() : '',
      allocation_number: allocIdx >= 0 ? (cols[allocIdx] || '').trim() : '',
    }
  }).filter(r => r.rdc && r.article_number && parseFloat(r.do_qty) > 0)
}

export default function PendingDeliveryOrderPage() {
  const [rows, setRows]             = useState([{ ...EMPTY_ROW }])
  const [submitting, setSubmitting] = useState(false)
  const [progress, setProgress]     = useState(null)  // { sent, total }
  const [result, setResult]         = useState(null)
  const [history, setHistory]       = useState([])
  const [histLoading, setHistLoading] = useState(false)
  // Bulk mode — for huge CSV uploads. Rows live in a ref to avoid re-rendering
  // 200k <input>s and freezing the browser.
  const bulkRowsRef                 = useRef(null)
  const [bulkMode, setBulkMode]     = useState(false)
  const [bulkCount, setBulkCount]   = useState(0)
  const [bulkPreview, setBulkPreview] = useState([])
  const fileRef = useRef()

  const loadHistory = useCallback(async () => {
    setHistLoading(true)
    try {
      const { data } = await pendAlcAPI.doHistory(50)
      setHistory(data?.data || [])
    } catch { /* silent */ }
    finally { setHistLoading(false) }
  }, [])

  useEffect(() => { loadHistory() }, [loadHistory])

  const setRow = (i, field, val) => {
    setRows(prev => prev.map((r, idx) => idx === i ? { ...r, [field]: val } : r))
  }

  const addRow = () => setRows(prev => [...prev, { ...EMPTY_ROW }])

  const removeRow = (i) => setRows(prev => prev.filter((_, idx) => idx !== i))

  const exitBulkMode = () => {
    bulkRowsRef.current = null
    setBulkMode(false)
    setBulkCount(0)
    setBulkPreview([])
    setRows([{ ...EMPTY_ROW }])
  }

  const handleFile = (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = ev => {
      const parsed = parseCsv(ev.target.result)
      if (!parsed) {
        toast.error('CSV must have columns: RDC (or "Receiving Store"), Article_Number (or "Material No"), DO_QTY')
        return
      }
      if (parsed.length === 0) {
        toast.error('No valid rows found in CSV')
        return
      }
      if (parsed.length > BULK_THRESHOLD) {
        bulkRowsRef.current = parsed
        setBulkMode(true)
        setBulkCount(parsed.length)
        setBulkPreview(parsed.slice(0, 5))
        toast.success(`Loaded ${parsed.length.toLocaleString()} rows in bulk mode`)
      } else {
        setRows(parsed)
        toast.success(`Loaded ${parsed.length} rows from CSV`)
      }
    }
    reader.readAsText(file)
    e.target.value = ''
  }

  const buildPayload = (r) => ({
    rdc:            r.rdc.trim(),
    article_number: r.article_number.trim(),
    do_qty:         parseFloat(r.do_qty),
    ...(r.st_cd?.trim()             ? { st_cd:             r.st_cd.trim() }             : {}),
    ...(r.do_number?.trim()         ? { do_number:         r.do_number.trim() }         : {}),
    ...(r.allocation_number?.trim() ? { allocation_number: r.allocation_number.trim() } : {}),
  })

  const handleSubmit = async () => {
    const sourceRows = bulkMode ? (bulkRowsRef.current || []) : rows
    const valid = sourceRows.filter(
      r => r.rdc && r.article_number && parseFloat(r.do_qty) > 0
    )
    if (!valid.length) {
      toast.error('No valid rows to submit (check RDC, Article Number, and DO QTY > 0)')
      return
    }
    setSubmitting(true)
    setResult(null)
    setProgress({ sent: 0, total: valid.length })

    const sessionId  = newSessionId()
    const totalRows  = valid.length
    const numChunks  = Math.ceil(totalRows / SUBMIT_CHUNK)
    let totalUpdated = 0
    const failures   = []  // {chunkIdx, rows, message}

    // Per-chunk try/catch: one failure no longer abandons the whole upload.
    // Every chunk gets a fair attempt; the user sees a summary at the end.
    // Async path: kick off background job per chunk, poll until complete,
    // then move to the next chunk. Survives the 100s Cloudflare timeout.
    const waitForJob = (jobId) => new Promise((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const s = await pendAlcAPI.asyncJobStatus(jobId)
          const j = s.data?.data
          if (!j) return
          if (j.status === 'completed') { clearInterval(timer); resolve(j) }
          else if (j.status === 'failed') {
            clearInterval(timer)
            reject(new Error(j.error || 'job failed'))
          }
        } catch (err) { clearInterval(timer); reject(err) }
      }, 2000)
    })

    for (let i = 0, idx = 0; i < totalRows; i += SUBMIT_CHUNK, idx += 1) {
      const slice   = valid.slice(i, i + SUBMIT_CHUNK)
      const payload = {
        rows:           slice.map(buildPayload),
        session_id:     sessionId,
        is_first_chunk: idx === 0,
        is_last_chunk:  idx === numChunks - 1,
      }
      try {
        const startResp = await pendAlcAPI.doUpdateAsync(payload)
        const jobId = startResp.data?.job_id
        if (!jobId) throw new Error('No job_id from server')
        const finalJob = await waitForJob(jobId)
        totalUpdated += (finalJob.result?.updated_rows || 0)
      } catch (e) {
        failures.push({
          chunkIdx: idx + 1,
          rows:     slice.length,
          message:  e.response?.data?.detail || e.message || 'request failed',
        })
      }
      setProgress({ sent: Math.min(i + slice.length, totalRows), total: totalRows })
    }

    setResult({ submitted: totalRows, updated: totalUpdated, failures: failures.length })
    if (failures.length === 0) {
      toast.success(`DO update applied — ${totalUpdated.toLocaleString()} ARS_PEND_ALC rows updated`)
      if (bulkMode) exitBulkMode()
      else setRows([{ ...EMPTY_ROW }])
    } else {
      const failedRows = failures.reduce((s, f) => s + f.rows, 0)
      toast.error(
        `Partial: ${totalUpdated.toLocaleString()} pend_alc rows updated, ` +
        `${failures.length}/${numChunks} chunk(s) failed (${failedRows.toLocaleString()} rows). ` +
        `First error: ${failures[0].message}`,
        { duration: 8000 }
      )
    }
    loadHistory()
    setSubmitting(false)
    setProgress(null)
  }

  const submittable = useMemo(() => {
    if (bulkMode) return bulkCount > 0
    return rows.some(r => r.rdc && r.article_number && parseFloat(r.do_qty) > 0)
  }, [bulkMode, bulkCount, rows])

  const fmt = (n) => typeof n === 'number'
    ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : '—'

  const _inp = {
    fontSize: 10, padding: '4px 7px', border: `1px solid ${C.border}`,
    borderRadius: 4, width: '100%', outline: 'none',
  }

  return (
    <div style={{ padding: '16px 20px', fontFamily: 'Inter,system-ui,sans-serif',
                  fontSize: 11, color: C.text, background: C.bg, minHeight: '100vh' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <Truck size={16} color={C.primary}/>
        <div style={{ fontSize: 13, fontWeight: 800, color: C.text }}>Daily DO Entry</div>
        <div style={{ fontSize: 10, color: C.textMuted }}>
          Record SAP Delivery Order quantities to update pending allocation balances
        </div>
      </div>

      {/* Upload CSV */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    padding: 12, marginBottom: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em' }}>
            IMPORT FROM CSV
          </div>
          <div style={{ fontSize: 9, color: C.textMuted }}>
            Required: RDC, Article_Number, DO_QTY — optional: ST_CD (dest store), DO_Number, Allocation_Number (links DO back to its BDC)
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <button onClick={() => fileRef.current?.click()}
            style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10,
                     padding: '5px 12px', borderRadius: 4, border: `1px solid ${C.primary}`,
                     background: '#fff', color: C.primary, cursor: 'pointer', fontWeight: 600 }}>
            <Upload size={12}/> Choose CSV
          </button>
          <button onClick={() => {
            const csv = '﻿'
              + 'RDC,ST_CD,Article_Number,DO_QTY,DO_Number,Allocation_Number\n'
              + 'DW01,S001,1000000001,50,DO-2026-001,2526-001\n'
              + 'DW01,S002,1000000001,30,DO-2026-001,2526-001\n'
              + 'DW02,S003,1000000002,120,DO-2026-002,2526-002\n'
            const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
            const url = URL.createObjectURL(blob)
            const a = document.createElement('a')
            a.href = url
            a.download = 'DO_Entry_Template.csv'
            document.body.appendChild(a)
            a.click()
            document.body.removeChild(a)
            setTimeout(() => URL.revokeObjectURL(url), 1000)
          }} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10,
                      padding: '5px 12px', borderRadius: 4, border: `1px solid ${C.border}`,
                      background: '#fff', color: C.textSub, cursor: 'pointer', fontWeight: 600 }}>
            <Download size={12}/> Sample Template
          </button>
          {!bulkMode && (
            <div style={{ fontSize: 9, color: C.textMuted }}>
              or enter rows manually below
            </div>
          )}
          <input ref={fileRef} type="file" accept=".csv" onChange={handleFile}
            style={{ display: 'none' }}/>
        </div>
      </div>

      {/* BULK MODE — summary + preview, no editable table */}
      {bulkMode && (
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                      padding: 16, marginBottom: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <Database size={16} color={C.primary}/>
            <div>
              <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
                Bulk upload mode — {bulkCount.toLocaleString()} rows ready
              </div>
              <div style={{ fontSize: 10, color: C.textMuted, marginTop: 2 }}>
                Editable table is hidden for {'>'}{BULK_THRESHOLD} rows. Submitted in batches of {SUBMIT_CHUNK.toLocaleString()}.
              </div>
            </div>
            <div style={{ flex: 1 }}/>
            <button onClick={exitBulkMode} disabled={submitting}
              style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 10,
                       padding: '4px 10px', borderRadius: 4, border: `1px solid ${C.border}`,
                       background: '#fff', color: C.textSub, cursor: 'pointer',
                       opacity: submitting ? 0.5 : 1 }}>
              <X size={11}/> Clear & Switch to Manual
            </button>
          </div>
          <div style={{ fontSize: 9, fontWeight: 700, color: C.textSub,
                        letterSpacing: '.05em', marginBottom: 6 }}>
            <FileText size={10} style={{ display: 'inline', marginRight: 4, verticalAlign: 'middle' }}/>
            FIRST 5 ROWS (preview)
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
              <thead>
                <tr style={{ background: C.bg }}>
                  {['RDC','ST_CD','Article','DO Qty','DO Number','Alloc No'].map(h => (
                    <th key={h} style={{ padding: '5px 8px', textAlign: 'left',
                                          fontSize: 9, fontWeight: 700, color: C.textSub,
                                          letterSpacing: '.05em',
                                          borderBottom: `1px solid ${C.border}` }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bulkPreview.map((r, i) => (
                  <tr key={i} style={{ borderBottom: `1px solid ${C.border}` }}>
                    <td style={{ padding: '4px 8px' }}>{r.rdc}</td>
                    <td style={{ padding: '4px 8px' }}>{r.st_cd}</td>
                    <td style={{ padding: '4px 8px', fontFamily: 'monospace' }}>{r.article_number}</td>
                    <td style={{ padding: '4px 8px', textAlign: 'right' }}>{r.do_qty}</td>
                    <td style={{ padding: '4px 8px', fontFamily: 'monospace', color: C.textMuted }}>{r.do_number}</td>
                    <td style={{ padding: '4px 8px', fontFamily: 'monospace', color: C.textMuted }}>{r.allocation_number}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {bulkCount > 5 && (
            <div style={{ fontSize: 9, color: C.textMuted, marginTop: 6, fontStyle: 'italic' }}>
              … {(bulkCount - 5).toLocaleString()} more rows hidden
            </div>
          )}
          {/* Submit button for bulk mode */}
          <div style={{ display: 'flex', gap: 8, marginTop: 14, alignItems: 'center' }}>
            <div style={{ flex: 1 }}/>
            <button onClick={handleSubmit} disabled={submitting || !submittable}
              style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, fontWeight: 700,
                       padding: '8px 22px', borderRadius: 5, border: 'none',
                       background: (submitting || !submittable) ? C.textMuted : C.primary,
                       color: '#fff', cursor: (submitting || !submittable) ? 'not-allowed' : 'pointer' }}>
              {submitting
                ? <><RefreshCw size={12} style={{ animation: 'spin 1s linear infinite' }}/> Submitting…</>
                : <><Send size={12}/> Submit DO Update ({bulkCount.toLocaleString()} rows)</>}
            </button>
          </div>
        </div>
      )}

      {/* Progress bar (shown during submit) */}
      {progress && (
        <div style={{ background: C.card, border: `1px solid ${C.primary}`, borderRadius: 8,
                      padding: 12, marginBottom: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between',
                        fontSize: 10, fontWeight: 600, color: C.text, marginBottom: 6 }}>
            <span>Uploading…</span>
            <span>{progress.sent.toLocaleString()} / {progress.total.toLocaleString()} rows</span>
          </div>
          <div style={{ background: C.bg, borderRadius: 4, overflow: 'hidden', height: 6 }}>
            <div style={{
              width: `${(progress.sent / progress.total) * 100}%`,
              height: '100%', background: C.primary,
              transition: 'width 0.3s ease',
            }}/>
          </div>
        </div>
      )}

      {/* MANUAL MODE — editable table */}
      {!bulkMode && (
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    padding: 12, marginBottom: 12 }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub,
                      letterSpacing: '.05em', marginBottom: 8 }}>DELIVERY ORDER ROWS</div>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ background: C.bg }}>
              {['RDC (Source WH)', 'ST_CD (Dest Store)', 'Article Number (VAR_ART)', 'DO Qty', 'DO Number (optional)', 'Allocation No. (optional)', ''].map((h, i) => (
                <th key={i} style={{ padding: '6px 8px', textAlign: 'left', fontSize: 9,
                                     fontWeight: 700, color: C.textSub, letterSpacing: '.05em',
                                     borderBottom: `1px solid ${C.border}` }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} style={{ borderBottom: `1px solid ${C.border}` }}>
                <td style={{ padding: '4px 6px', width: '18%' }}>
                  <input value={r.rdc} onChange={e => setRow(i, 'rdc', e.target.value)}
                    placeholder="e.g. DW01" style={_inp}/>
                </td>
                <td style={{ padding: '4px 6px', width: '15%' }}>
                  <input value={r.st_cd || ''} onChange={e => setRow(i, 'st_cd', e.target.value)}
                    placeholder="e.g. S001" style={_inp}/>
                </td>
                <td style={{ padding: '4px 6px', width: '30%' }}>
                  <input value={r.article_number}
                    onChange={e => setRow(i, 'article_number', e.target.value)}
                    placeholder="e.g. 1234567890" style={{ ..._inp, fontFamily: 'monospace' }}/>
                </td>
                <td style={{ padding: '4px 6px', width: '12%' }}>
                  <input type="number" min="0" step="1"
                    value={r.do_qty} onChange={e => setRow(i, 'do_qty', e.target.value)}
                    placeholder="0" style={{ ..._inp, textAlign: 'right' }}/>
                </td>
                <td style={{ padding: '4px 6px', width: '15%' }}>
                  <input value={r.do_number || ''} onChange={e => setRow(i, 'do_number', e.target.value)}
                    placeholder="e.g. DO-2026-001" style={{ ..._inp, fontFamily: 'monospace', fontSize: 9 }}/>
                </td>
                <td style={{ padding: '4px 6px', width: '13%' }}>
                  <input value={r.allocation_number || ''} onChange={e => setRow(i, 'allocation_number', e.target.value)}
                    placeholder="e.g. 2526-001" style={{ ..._inp, fontFamily: 'monospace', fontSize: 9 }}/>
                </td>
                <td style={{ padding: '4px 6px', textAlign: 'center' }}>
                  {rows.length > 1 && (
                    <button onClick={() => removeRow(i)}
                      style={{ background: 'none', border: 'none', cursor: 'pointer',
                               color: C.red, padding: 2 }}>
                      <Trash2 size={12}/>
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'center' }}>
          <button onClick={addRow}
            style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 10,
                     padding: '4px 10px', borderRadius: 4, border: `1px solid ${C.border}`,
                     background: '#fff', color: C.textSub, cursor: 'pointer' }}>
            <Plus size={11}/> Add Row
          </button>
          <div style={{ flex: 1 }}/>
          <button onClick={handleSubmit} disabled={submitting || !submittable}
            style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, fontWeight: 700,
                     padding: '6px 18px', borderRadius: 5, border: 'none',
                     background: (submitting || !submittable) ? C.textMuted : C.primary,
                     color: '#fff', cursor: (submitting || !submittable) ? 'not-allowed' : 'pointer' }}>
            {submitting
              ? <><RefreshCw size={12} style={{ animation: 'spin 1s linear infinite' }}/> Updating…</>
              : <><Send size={12}/> Submit DO Update</>}
          </button>
        </div>
      </div>
      )}

      {/* Result banner */}
      {result && (
        <div style={{
            background: result.failures ? '#fef2f2' : '#f0fdf4',
            border: `1px solid ${result.failures ? '#fecaca' : '#bbf7d0'}`,
            borderRadius: 8,
            padding: '10px 14px', marginBottom: 12,
            display: 'flex', alignItems: 'center', gap: 8 }}>
          <CheckCircle size={14} color={result.failures ? C.red : C.green}/>
          <span style={{ fontSize: 10, fontWeight: 600,
                         color: result.failures ? C.red : C.green }}>
            DO update {result.failures ? 'partially applied' : 'applied'} —
            &nbsp;{result.submitted.toLocaleString()} rows submitted,&nbsp;
            {result.updated.toLocaleString()} ARS_PEND_ALC rows updated
            {result.failures
              ? ` (${result.failures} chunk${result.failures > 1 ? 's' : ''} failed — see toast)`
              : ''}
          </span>
        </div>
      )}

      {/* History */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    padding: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em' }}>
            RECENT DO DEDUCTIONS
          </div>
          <button onClick={loadHistory} disabled={histLoading}
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.textMuted }}>
            <RefreshCw size={11} style={{ animation: histLoading ? 'spin 1s linear infinite' : 'none' }}/>
          </button>
        </div>
        {histLoading ? (
          <div style={{ padding: 20, textAlign: 'center', color: C.textMuted }}>Loading…</div>
        ) : !history.length ? (
          <div style={{ padding: 20, textAlign: 'center', color: C.textMuted }}>
            No DO deductions recorded yet.
          </div>
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
            <thead>
              <tr style={{ background: C.bg }}>
                {['RDC','ARTICLE','MAJ_CAT','ALLOC','DO QTY','PEND','STATUS','LAST DO'].map(h => (
                  <th key={h} style={{ padding: '6px 10px', textAlign: 'left', fontSize: 9,
                                       fontWeight: 700, color: C.textSub, letterSpacing: '.05em',
                                       borderBottom: `1px solid ${C.border}` }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {history.map((r, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${C.border}`,
                                     background: r.is_closed ? '#f0fdf4' : (i % 2 === 0 ? '#fff' : C.bg) }}>
                  <td style={{ padding: '5px 10px' }}>{r.rdc}</td>
                  <td style={{ padding: '5px 10px', fontFamily: 'monospace', fontSize: 9 }}>
                    {r.article_number}
                  </td>
                  <td style={{ padding: '5px 10px' }}>{r.maj_cat || '—'}</td>
                  <td style={{ padding: '5px 10px', textAlign: 'right' }}>{fmt(r.alloc_qty)}</td>
                  <td style={{ padding: '5px 10px', textAlign: 'right', color: C.green }}>
                    {fmt(r.do_qty)}
                  </td>
                  <td style={{ padding: '5px 10px', textAlign: 'right', fontWeight: 700,
                               color: r.pend_qty > 0 ? C.amber : C.green }}>
                    {fmt(r.pend_qty)}
                  </td>
                  <td style={{ padding: '5px 10px' }}>
                    <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
                                   background: r.is_closed ? '#dcfce7' : '#fef3c7',
                                   color: r.is_closed ? C.green : C.amber }}>
                      {r.is_closed ? 'CLOSED' : 'OPEN'}
                    </span>
                  </td>
                  <td style={{ padding: '5px 10px', fontSize: 9, color: C.textMuted, whiteSpace: 'nowrap' }}>
                    {r.last_do_at ? r.last_do_at.slice(0, 16).replace('T', ' ') : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}
