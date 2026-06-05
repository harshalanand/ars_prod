/**
 * AdhocClosePage — Adhoc-close PEND_ALC rows + cancel their open BDC.
 *
 * Use case: bot stamped a BDC for an article that has no MSA stock (or for
 * any other reason the line should not ship). Closing here:
 *   - sets ARS_PEND_ALC.IS_CLOSED=1 on every open row matching the key
 *   - flips ARS_BDC_HISTORY.STATUS='CANCELLED' on every still-OPEN row at
 *     the same key, releasing `_NO_OPEN_BDC_PREDICATE` so a corrected
 *     re-BDC can flow.
 *
 * Logged as OP_TYPE='ADHOC_CLOSE' — revertable from the Operations Log.
 */
import { useState, useRef } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Upload, Plus, Trash2, Send, XCircle, FileText, AlertTriangle, RefreshCw,
} from 'lucide-react'

const C = {
  primary: '#4f46e5', blue: '#0891b2', green: '#16a34a',
  amber: '#d97706', red: '#dc2626', text: '#1e293b',
  textSub: '#64748b', textMuted: '#94a3b8', border: '#e2e8f0',
  bg: '#f8fafc', card: '#ffffff',
}

const EMPTY_ROW = { rdc: '', st_cd: '', article_number: '', reason: '' }

function parseCsv(text) {
  const lines = text.trim().split('\n')
  if (lines.length < 2) return []
  const norm = h => h.trim().toLowerCase()
                     .replace(/^﻿/, '')
                     .replace(/[\s\-.]+/g, '_')
                     .replace(/_+/g, '_')
  const hdr = lines[0].split(',').map(norm)
  const idx = (...names) => hdr.findIndex(h => names.includes(h))
  const rdcIdx = idx('rdc', 'source_warehouse', 'warehouse', 'wh')
  const stIdx  = idx('st_cd', 'st_code', 'store', 'store_code', 'werks')
  const artIdx = idx('article_number', 'article', 'material_no', 'material', 'matnr', 'var_art')
  const remIdx = idx('reason', 'remark', 'remarks', 'note', 'notes', 'comment')
  if (rdcIdx < 0 || artIdx < 0) return null
  const out = []
  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i].split(',')
    const get = (i) => i >= 0 ? (cols[i] || '').trim() : ''
    const r = {
      rdc:            get(rdcIdx),
      st_cd:          get(stIdx),
      article_number: get(artIdx),
      reason:         get(remIdx),
    }
    if (r.rdc && r.article_number) out.push(r)
  }
  return out
}

export default function AdhocClosePage() {
  const [rows, setRows]             = useState([{ ...EMPTY_ROW }])
  const [globalReason, setGlobalReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult]         = useState(null)
  // File upload mode (no row editor needed)
  const [file, setFile]             = useState(null)
  const [fileRows, setFileRows]     = useState(null)
  const fileRef = useRef()

  const setRow = (i, k, v) => setRows(prev => {
    const next = [...prev]; next[i] = { ...next[i], [k]: v }; return next
  })
  const addRow    = () => setRows(prev => [...prev, { ...EMPTY_ROW }])
  const removeRow = (i) => setRows(prev => prev.filter((_, j) => j !== i))
  const clearAll  = () => setRows([{ ...EMPTY_ROW }])

  const validRows = rows.filter(r => r.rdc.trim() && r.article_number.trim())

  // CSV → preview only (don't auto-submit — let the user click Submit File)
  const onPickFile = (e) => {
    const f = e.target.files?.[0]
    setFile(f || null); setFileRows(null); setResult(null)
    if (!f) return
    if (!/\.(csv)$/i.test(f.name)) {
      // Excel uploads go through the backend (it handles xlsx). We still
      // give a quick row-count preview for CSV; for Excel we just show the
      // filename and trust the backend.
      return
    }
    const reader = new FileReader()
    reader.onload = (ev) => {
      const parsed = parseCsv(String(ev.target?.result || ''))
      if (parsed === null) {
        toast.error('CSV missing required columns: RDC, ARTICLE_NUMBER')
        setFile(null); if (fileRef.current) fileRef.current.value = ''
        return
      }
      setFileRows(parsed)
    }
    reader.readAsText(f)
  }

  const submitRows = async () => {
    if (validRows.length === 0) {
      toast.error('Add at least one row with RDC + ARTICLE_NUMBER')
      return
    }
    if (!confirm(
      `Adhoc-close ${validRows.length} key(s)?\n\n` +
      `This will set IS_CLOSED=1 on every matching open PEND_ALC row ` +
      `and cancel any STATUS='OPEN' BDC history rows. Revertable from ` +
      `the Operations Log.`
    )) return

    setSubmitting(true); setResult(null)
    try {
      const { data } = await pendAlcAPI.closeRows({
        reason: globalReason.trim() || null,
        rows: validRows.map(r => ({
          rdc:            r.rdc.trim(),
          article_number: r.article_number.trim(),
          ...(r.st_cd?.trim()  ? { st_cd:  r.st_cd.trim() }  : {}),
          ...(r.reason?.trim() ? { reason: r.reason.trim() } : {}),
        })),
      })
      setResult(data)
      toast.success(
        `Closed ${data.pend_rows_closed} PEND rows · ` +
        `cancelled ${data.history_rows_cancelled} BDC history rows`
      )
      clearAll()
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Adhoc close failed')
    } finally {
      setSubmitting(false)
    }
  }

  const submitFile = async () => {
    if (!file) return
    const count = fileRows?.length
    const msg = count != null
      ? `Adhoc-close ${count} key(s) from ${file.name}?`
      : `Submit ${file.name} for adhoc close?`
    if (!confirm(`${msg}\n\nRevertable from the Operations Log.`)) return

    setSubmitting(true); setResult(null)
    try {
      const { data } = await pendAlcAPI.closeRowsFile(
        file, globalReason.trim() || null
      )
      setResult(data)
      toast.success(
        `Closed ${data.pend_rows_closed} PEND rows · ` +
        `cancelled ${data.history_rows_cancelled} BDC history rows`
      )
      setFile(null); setFileRows(null)
      if (fileRef.current) fileRef.current.value = ''
    } catch (e) {
      toast.error(e.response?.data?.detail || 'File upload failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div style={{ padding: '16px 20px', fontFamily: 'Inter,system-ui,sans-serif',
                  fontSize: 11, color: C.text, background: C.bg, minHeight: '100vh' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <XCircle size={16} color={C.red}/>
        <div>
          <div style={{ fontSize: 13, fontWeight: 800 }}>Adhoc Close</div>
          <div style={{ fontSize: 10, color: C.textMuted }}>
            Close PEND_ALC rows + cancel their open BDC history.
            Use when a BDC line was generated but should not ship
            (e.g. no MSA stock).
          </div>
        </div>
      </div>

      {/* Global reason — applied to every row that doesn't carry its own */}
      <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                    padding: 10, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 10, fontWeight: 600, color: C.textSub }}>Reason (applies to all):</span>
        <input
          value={globalReason}
          onChange={e => setGlobalReason(e.target.value)}
          placeholder="e.g. No MSA stock — bot mis-allocated"
          style={{ flex: 1, fontSize: 11, padding: '5px 8px',
                   border: `1px solid ${C.border}`, borderRadius: 4 }}
        />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 12 }}>

        {/* Manual row entry */}
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, overflow: 'hidden' }}>
          <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`,
                        display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em' }}>
              ADHOC ENTRIES ({validRows.length} valid of {rows.length})
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              <button onClick={addRow}
                style={{ fontSize: 10, padding: '4px 8px', border: `1px solid ${C.border}`,
                         background: '#fff', color: C.primary, borderRadius: 4, cursor: 'pointer',
                         display: 'flex', alignItems: 'center', gap: 4 }}>
                <Plus size={11}/> Add row
              </button>
              <button onClick={clearAll} disabled={submitting}
                style={{ fontSize: 10, padding: '4px 8px', border: `1px solid ${C.border}`,
                         background: '#fff', color: C.textSub, borderRadius: 4, cursor: 'pointer' }}>
                Clear
              </button>
              <button onClick={submitRows} disabled={submitting || validRows.length === 0}
                style={{ fontSize: 10, padding: '4px 10px', border: 'none',
                         background: validRows.length === 0 ? C.textMuted : C.red,
                         color: '#fff', borderRadius: 4,
                         cursor: validRows.length === 0 || submitting ? 'not-allowed' : 'pointer',
                         display: 'flex', alignItems: 'center', gap: 4, fontWeight: 600 }}>
                <Send size={11}/> {submitting ? 'Closing…' : 'Close rows'}
              </button>
            </div>
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
            <thead><tr style={{ background: C.bg }}>
              <th style={th}>RDC*</th>
              <th style={th}>ST_CD <span style={{ color: C.textMuted, fontWeight: 400 }}>(blank = all stores)</span></th>
              <th style={th}>ARTICLE*</th>
              <th style={th}>REASON <span style={{ color: C.textMuted, fontWeight: 400 }}>(per-row override)</span></th>
              <th style={{ ...th, width: 30 }}></th>
            </tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${C.border}`,
                                     background: i % 2 === 0 ? '#fff' : C.bg }}>
                  <td style={td}><input value={r.rdc} onChange={e => setRow(i, 'rdc', e.target.value)}
                    style={inp} placeholder="DH24"/></td>
                  <td style={td}><input value={r.st_cd} onChange={e => setRow(i, 'st_cd', e.target.value)}
                    style={inp} placeholder="(blank)"/></td>
                  <td style={td}><input value={r.article_number}
                    onChange={e => setRow(i, 'article_number', e.target.value)}
                    style={inp} placeholder="1110112278002"/></td>
                  <td style={td}><input value={r.reason} onChange={e => setRow(i, 'reason', e.target.value)}
                    style={inp} placeholder="(uses global reason)"/></td>
                  <td style={{ ...td, textAlign: 'center' }}>
                    {rows.length > 1 && (
                      <button onClick={() => removeRow(i)}
                        style={{ background: 'none', border: 'none', cursor: 'pointer',
                                 color: C.red, padding: 2 }}>
                        <Trash2 size={11}/>
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* File upload */}
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, overflow: 'hidden' }}>
          <div style={{ padding: '8px 12px', borderBottom: `1px solid ${C.border}`,
                        fontSize: 10, fontWeight: 700, color: C.textSub, letterSpacing: '.05em' }}>
            BULK UPLOAD
          </div>
          <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ fontSize: 10, color: C.textSub }}>
              CSV or Excel with columns: <code style={{ background: C.bg, padding: '0 4px', borderRadius: 3 }}>
              RDC, ARTICLE_NUMBER</code><br/>
              Optional: <code style={{ background: C.bg, padding: '0 4px', borderRadius: 3 }}>ST_CD, REASON</code>
            </div>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,.xlsx,.xls"
              onChange={onPickFile}
              style={{ fontSize: 10 }}
            />
            {file && (
              <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: 4,
                            padding: 8, fontSize: 10, display: 'flex', alignItems: 'center', gap: 6 }}>
                <FileText size={12} color={C.blue}/>
                <span style={{ flex: 1 }}>{file.name}</span>
                {fileRows != null && (
                  <span style={{ color: C.textMuted }}>{fileRows.length.toLocaleString()} rows</span>
                )}
              </div>
            )}
            <button onClick={submitFile} disabled={!file || submitting}
              style={{ padding: '6px 10px', border: 'none',
                       background: !file ? C.textMuted : C.red, color: '#fff', borderRadius: 4,
                       cursor: !file || submitting ? 'not-allowed' : 'pointer',
                       display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
                       fontSize: 10, fontWeight: 600 }}>
              <Upload size={11}/> {submitting ? 'Uploading…' : 'Submit file'}
            </button>
            <div style={{ fontSize: 9, color: C.textMuted, marginTop: 4 }}>
              <AlertTriangle size={9} style={{ verticalAlign: 'middle' }}/> Adhoc closes are
              logged as <code>ADHOC_CLOSE</code> in the Operations Log and can be reverted
              from there until a downstream BDC/DO touches the same keys.
            </div>
          </div>
        </div>
      </div>

      {/* Result */}
      {result && (
        <div style={{ marginTop: 12, background: C.card, border: `1px solid ${C.green}`,
                      borderRadius: 8, padding: 12, display: 'flex', alignItems: 'center', gap: 10 }}>
          <RefreshCw size={14} color={C.green}/>
          <div style={{ flex: 1, fontSize: 10 }}>
            <div style={{ fontWeight: 700, color: C.green }}>
              Adhoc close applied · op_key {result.op_key}
            </div>
            <div style={{ color: C.textSub, marginTop: 2 }}>
              {result.input_keys} key(s) submitted ·
              <span style={{ color: C.amber, fontWeight: 600 }}> {result.pend_rows_closed}</span> PEND rows closed ·
              <span style={{ color: C.red, fontWeight: 600 }}> {result.history_rows_cancelled}</span> BDC history rows cancelled.
              Revertable from <a href="/pend-alc/operations" style={{ color: C.primary }}>Operations Log</a>.
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

const th  = { padding: '6px 8px', textAlign: 'left', fontWeight: 700, color: C.textSub,
              fontSize: 9, letterSpacing: '.04em', borderBottom: `1px solid ${C.border}` }
const td  = { padding: '4px 6px' }
const inp = { width: '100%', fontSize: 10, padding: '4px 6px',
              border: `1px solid ${C.border}`, borderRadius: 3 }
