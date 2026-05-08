/**
 * ManualPendAlcPage — Manual Pending Allocation entry tool
 * Insert allocation rows directly into ARS_PEND_ALC (SOURCE='MANUAL').
 * The backend automatically adjusts ARS_MSA_TOTAL/GEN_ART/VAR_ART for the
 * affected (RDC, ARTICLE) keys: PEND_QTY goes up, FNL_Q goes down — same
 * for both manual entry and bulk CSV upload.
 *
 * Two modes:
 *   - manual: small ad-hoc entry (≤ BULK_THRESHOLD rows). Renders editable table.
 *   - bulk:   large CSV upload. Skips table render entirely, shows summary +
 *             head/tail preview. Required because rendering 200k <input>s
 *             freezes the browser.
 */
import { useState, useRef, useMemo } from 'react'
import { pendAlcAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Upload, Plus, Trash2, Send, RefreshCw, CheckCircle, ClipboardList,
  Download, Database, X, FileText,
} from 'lucide-react'

const C = {
  primary: '#4f46e5', blue: '#0891b2', green: '#16a34a',
  amber: '#d97706', red: '#dc2626', text: '#1e293b',
  textSub: '#64748b', textMuted: '#94a3b8', border: '#e2e8f0',
  bg: '#f8fafc', card: '#ffffff',
}

const EMPTY_ROW = {
  rdc: '', st_cd: '', article_number: '', alloc_qty: '',
  maj_cat: '', gen_art_number: '', clr: '', remarks: '',
}

// Above this row count, switch to bulk mode (no table render).
const BULK_THRESHOLD = 100

// Network payload chunking — submit large bulk uploads in batches so the
// browser doesn't try to encode 30+ MB of JSON in one go and the user sees
// progress instead of a frozen tab.
const SUBMIT_CHUNK = 5000

function parseCsv(text) {
  const lines = text.trim().split('\n')
  if (lines.length < 2) return []
  const hdr = lines[0].split(',').map(h => h.trim().toLowerCase().replace(/^﻿/, ''))
  const idx = (...names) => hdr.findIndex(h => names.includes(h))
  const rdcIdx  = idx('rdc','source warehouse','source wh','warehouse','wh')
  const stIdx   = idx('st_cd','st code','store','store code','dest store','destination store','werks')
  const artIdx  = idx('article_number','material no','material','matnr','var_art')
  const qtyIdx  = idx('alloc_qty','allocation qty','qty','quantity','alloc')
  const mcIdx   = idx('maj_cat','major category','majcat')
  const ganIdx  = idx('gen_art_number','gen art','generic article','gen_art')
  const clrIdx  = idx('clr','colour','color')
  const remIdx  = idx('remarks','remark','notes','note','comment')
  if (rdcIdx < 0 || artIdx < 0 || qtyIdx < 0) return null
  const out = []
  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i].split(',')
    const get = (i) => i >= 0 ? (cols[i] || '').trim() : ''
    const r = {
      rdc:            get(rdcIdx),
      st_cd:          get(stIdx),
      article_number: get(artIdx),
      alloc_qty:      get(qtyIdx),
      maj_cat:        get(mcIdx),
      gen_art_number: get(ganIdx),
      clr:            get(clrIdx),
      remarks:        get(remIdx),
    }
    if (r.rdc && r.article_number && parseFloat(r.alloc_qty) > 0) out.push(r)
  }
  return out
}

function buildPayload(r) {
  return {
    rdc:            r.rdc.trim(),
    article_number: r.article_number.trim(),
    alloc_qty:      parseFloat(r.alloc_qty),
    ...(r.st_cd?.trim()           ? { st_cd:          r.st_cd.trim() }          : {}),
    ...(r.maj_cat?.trim()          ? { maj_cat:        r.maj_cat.trim() }        : {}),
    ...(r.gen_art_number?.trim()   ? { gen_art_number: r.gen_art_number.trim() } : {}),
    ...(r.clr?.trim()              ? { clr:            r.clr.trim() }            : {}),
    ...(r.remarks?.trim()          ? { remarks:        r.remarks.trim() }        : {}),
  }
}

export default function ManualPendAlcPage() {
  // Manual mode rows live in state (small list, drives the editable table).
  const [rows, setRows]             = useState([{ ...EMPTY_ROW }])
  // Bulk mode rows live in a ref to avoid React re-rendering on huge arrays.
  const bulkRowsRef                 = useRef(null)
  const [bulkMode, setBulkMode]     = useState(false)
  const [bulkCount, setBulkCount]   = useState(0)
  const [bulkPreview, setBulkPreview] = useState([])  // first 5 rows for display

  const [submitting, setSubmitting] = useState(false)
  const [progress, setProgress]     = useState(null) // { sent, total }
  const [result, setResult]         = useState(null)
  const fileRef = useRef()

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
        toast.error('CSV must have columns: RDC, Article_Number, Alloc_Qty (optional: ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, Remarks)')
        return
      }
      if (parsed.length === 0) {
        toast.error('No valid rows found in CSV')
        return
      }
      if (parsed.length > BULK_THRESHOLD) {
        // Bulk mode — keep rows out of React state, just summary + preview
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

  const downloadSample = () => {
    const csv = '﻿'
      + 'RDC,ST_CD,Article_Number,Alloc_Qty,MAJ_CAT,GEN_ART_NUMBER,CLR,Remarks\n'
      + 'DW01,S001,1000000001,50,FOOTWEAR,90000001,RED,Manual top-up\n'
      + 'DW01,S002,1000000001,30,FOOTWEAR,90000001,RED,Manual top-up\n'
      + 'DW02,S003,1000000002,120,APPAREL,90000002,BLUE,\n'
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'Manual_Alloc_Template.csv'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  const handleSubmit = async () => {
    const sourceRows = bulkMode ? (bulkRowsRef.current || []) : rows
    const valid = sourceRows.filter(
      r => r.rdc && r.article_number && parseFloat(r.alloc_qty) > 0
    )
    if (!valid.length) {
      toast.error('No valid rows to submit (check RDC, Article Number, and Alloc Qty > 0)')
      return
    }
    setSubmitting(true)
    setResult(null)
    setProgress({ sent: 0, total: valid.length })

    try {
      let totalInserted = 0
      // Chunk submission — for huge uploads this gives progress feedback
      // and keeps any single request under typical body-size limits.
      for (let i = 0; i < valid.length; i += SUBMIT_CHUNK) {
        const slice = valid.slice(i, i + SUBMIT_CHUNK)
        const payload = slice.map(buildPayload)
        const { data } = await pendAlcAPI.manualUpload(payload)
        totalInserted += (data.inserted_rows || 0)
        setProgress({ sent: Math.min(i + slice.length, valid.length), total: valid.length })
      }
      setResult({ submitted: valid.length, inserted: totalInserted })
      toast.success(`Manual allocation submitted — ${totalInserted.toLocaleString()} rows inserted`)
      // Reset
      if (bulkMode) exitBulkMode()
      else setRows([{ ...EMPTY_ROW }])
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Manual upload failed')
    } finally {
      setSubmitting(false)
      setProgress(null)
    }
  }

  const _inp = {
    fontSize: 10, padding: '4px 7px', border: `1px solid ${C.border}`,
    borderRadius: 4, width: '100%', outline: 'none',
  }

  const submittable = useMemo(() => {
    if (bulkMode) return bulkCount > 0
    return rows.some(r => r.rdc && r.article_number && parseFloat(r.alloc_qty) > 0)
  }, [bulkMode, bulkCount, rows])

  return (
    <div style={{ padding: '16px 20px', fontFamily: 'Inter,system-ui,sans-serif',
                  fontSize: 11, color: C.text, background: C.bg, minHeight: '100vh' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <ClipboardList size={16} color={C.primary}/>
        <div style={{ fontSize: 13, fontWeight: 800, color: C.text }}>Manual Allocation Entry</div>
        <div style={{ fontSize: 10, color: C.textMuted }}>
          Insert allocation rows directly into ARS_PEND_ALC (SOURCE=MANUAL). MSA will pick these up on next run.
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
            Required: RDC, Article_Number, Alloc_Qty — optional: ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, Remarks
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <button onClick={() => fileRef.current?.click()} disabled={submitting}
            style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10,
                     padding: '5px 12px', borderRadius: 4, border: `1px solid ${C.primary}`,
                     background: '#fff', color: C.primary, cursor: 'pointer', fontWeight: 600,
                     opacity: submitting ? 0.5 : 1 }}>
            <Upload size={12}/> Choose CSV
          </button>
          <button onClick={downloadSample}
            style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10,
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
                Editable table is hidden for {'>'}{BULK_THRESHOLD} rows. Rows submit in batches of {SUBMIT_CHUNK.toLocaleString()}.
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

          {/* First 5 rows preview */}
          <div style={{ fontSize: 9, fontWeight: 700, color: C.textSub,
                        letterSpacing: '.05em', marginBottom: 6 }}>
            <FileText size={10} style={{ display: 'inline', marginRight: 4, verticalAlign: 'middle' }}/>
            FIRST 5 ROWS (preview)
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
              <thead>
                <tr style={{ background: C.bg }}>
                  {['RDC','ST_CD','Article','Qty','MAJ_CAT','GEN_ART','CLR','Remarks'].map(h => (
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
                    <td style={{ padding: '4px 8px', textAlign: 'right' }}>{r.alloc_qty}</td>
                    <td style={{ padding: '4px 8px' }}>{r.maj_cat}</td>
                    <td style={{ padding: '4px 8px', fontFamily: 'monospace' }}>{r.gen_art_number}</td>
                    <td style={{ padding: '4px 8px' }}>{r.clr}</td>
                    <td style={{ padding: '4px 8px', color: C.textMuted }}>{r.remarks}</td>
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
        </div>
      )}

      {/* MANUAL MODE — editable table */}
      {!bulkMode && (
        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8,
                      padding: 12, marginBottom: 12, overflowX: 'auto' }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textSub,
                        letterSpacing: '.05em', marginBottom: 8 }}>ALLOCATION ROWS</div>
          <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 1100 }}>
            <thead>
              <tr style={{ background: C.bg }}>
                {['RDC', 'ST_CD', 'Article Number', 'Alloc Qty', 'MAJ_CAT', 'GEN_ART_NUMBER', 'CLR', 'Remarks', ''].map((h, i) => (
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
                  <td style={{ padding: '4px 6px', width: '9%' }}>
                    <input value={r.rdc} onChange={e => setRow(i, 'rdc', e.target.value)}
                      placeholder="DW01" style={_inp}/>
                  </td>
                  <td style={{ padding: '4px 6px', width: '9%' }}>
                    <input value={r.st_cd} onChange={e => setRow(i, 'st_cd', e.target.value)}
                      placeholder="S001" style={_inp}/>
                  </td>
                  <td style={{ padding: '4px 6px', width: '15%' }}>
                    <input value={r.article_number}
                      onChange={e => setRow(i, 'article_number', e.target.value)}
                      placeholder="1234567890" style={{ ..._inp, fontFamily: 'monospace' }}/>
                  </td>
                  <td style={{ padding: '4px 6px', width: '9%' }}>
                    <input type="number" min="0" step="1"
                      value={r.alloc_qty} onChange={e => setRow(i, 'alloc_qty', e.target.value)}
                      placeholder="0" style={{ ..._inp, textAlign: 'right' }}/>
                  </td>
                  <td style={{ padding: '4px 6px', width: '12%' }}>
                    <input value={r.maj_cat} onChange={e => setRow(i, 'maj_cat', e.target.value)}
                      placeholder="FOOTWEAR" style={_inp}/>
                  </td>
                  <td style={{ padding: '4px 6px', width: '12%' }}>
                    <input value={r.gen_art_number} onChange={e => setRow(i, 'gen_art_number', e.target.value)}
                      placeholder="90000001" style={{ ..._inp, fontFamily: 'monospace' }}/>
                  </td>
                  <td style={{ padding: '4px 6px', width: '8%' }}>
                    <input value={r.clr} onChange={e => setRow(i, 'clr', e.target.value)}
                      placeholder="RED" style={_inp}/>
                  </td>
                  <td style={{ padding: '4px 6px', width: '20%' }}>
                    <input value={r.remarks} onChange={e => setRow(i, 'remarks', e.target.value)}
                      placeholder="optional note" style={_inp}/>
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

      {/* Submit button row */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
        <div style={{ flex: 1 }}/>
        <button onClick={handleSubmit} disabled={submitting || !submittable}
          style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 11, fontWeight: 700,
                   padding: '8px 22px', borderRadius: 5, border: 'none',
                   background: (submitting || !submittable) ? C.textMuted : C.primary,
                   color: '#fff', cursor: (submitting || !submittable) ? 'not-allowed' : 'pointer' }}>
          {submitting
            ? <><RefreshCw size={12} style={{ animation: 'spin 1s linear infinite' }}/> Submitting…</>
            : <><Send size={12}/> Submit Manual Allocation
                {bulkMode ? ` (${bulkCount.toLocaleString()} rows)` : ''}</>}
        </button>
      </div>

      {/* Result banner */}
      {result && (
        <div style={{ background: '#f0fdf4', border: `1px solid #bbf7d0`, borderRadius: 8,
                      padding: '10px 14px', marginBottom: 12,
                      display: 'flex', alignItems: 'center', gap: 8 }}>
          <CheckCircle size={14} color={C.green}/>
          <span style={{ fontSize: 10, fontWeight: 600, color: C.green }}>
            Manual allocation submitted — {result.submitted.toLocaleString()} rows submitted,&nbsp;
            {result.inserted.toLocaleString()} rows inserted into ARS_PEND_ALC
          </span>
        </div>
      )}

      <style>{`@keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}
