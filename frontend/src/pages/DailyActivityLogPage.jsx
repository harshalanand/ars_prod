/**
 * DailyActivityLogPage — "what did I do in ARS today", as review-ready pointers.
 *
 * Rolls the raw audit_log into one bullet per (ARS module × action) for a chosen
 * day, and lets a superadmin mark each pointer YES / NO (validate what they did).
 * Validations persist per reviewer + day. Superadmin only.
 *
 * Backend: GET /activity-log?date=... , POST /activity-log/validate
 */
import { useState, useEffect, useCallback } from 'react'
import { activityLogAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  ClipboardCheck, RefreshCw, Check, X, Clock, Copy, Users, User as UserIcon,
  Zap, Download, CheckSquare, Square,
} from 'lucide-react'

const C = {
  primary: '#4f46e5', green: '#16a34a', red: '#dc2626', blue: '#0891b2',
  amber: '#d97706', text: '#1e293b', textSub: '#64748b', textMuted: '#94a3b8',
  border: '#e2e8f0', bg: '#f8fafc', card: '#ffffff',
}

const todayISO = () => {
  const d = new Date()
  const off = d.getTimezoneOffset()
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10)
}

// "14:05" (24h) → "2:05 PM" to match Karma's Actual-Time inputs.
const to12h = (hhmm) => {
  if (!hhmm) return ''
  const [h, m] = hhmm.split(':').map(Number)
  const ap = h >= 12 ? 'PM' : 'AM'
  const h12 = h % 12 === 0 ? 12 : h % 12
  return `${h12}:${String(m).padStart(2, '0')} ${ap}`
}

// Best-guess Karma "Task Type" from the ARS module — reviewer vs maker vs analysis.
const karmaTaskType = (subject = '') => {
  const s = subject.toLowerCase()
  if (/(review|dashboard|checklist|audit)/.test(s)) return 'Reviewer'
  if (/(gap|report|analy|trend)/.test(s)) return 'Analysis'
  return 'Maker'
}

// Karma Daily-Execution column order (matches the app's grid, tab-separated so it
// pastes straight into the Karma table or a spreadsheet).
const KARMA_COLS = ['S.No', 'Actual Task', 'Task Type', 'Actual From', 'Actual To', 'Yes/No', 'Note']

const btn = (border, bg, fg) => ({
  display: 'inline-flex', alignItems: 'center', gap: 5, padding: '5px 10px',
  border: `1px solid ${border}`, background: bg, color: fg, borderRadius: 6,
  fontSize: 11, fontWeight: 600, cursor: 'pointer',
})

const STATUS_STYLE = {
  yes:     { bg: '#dcfce7', fg: C.green,  label: 'YES' },
  no:      { bg: '#fee2e2', fg: C.red,    label: 'NO' },
  pending: { bg: '#f1f5f9', fg: C.textSub, label: 'PENDING' },
}

export default function DailyActivityLogPage() {
  const [date, setDate]         = useState(todayISO())
  const [source, setSource]     = useState('both')   // both | dev | audit
  const [allUsers, setAllUsers] = useState(false)
  const [loading, setLoading]   = useState(false)
  const [data, setData]         = useState(null)
  const [saving, setSaving]     = useState('')   // item_key currently saving
  const [selected, setSelected] = useState(() => new Set())  // chosen item_keys
  const [showGen, setShowGen]   = useState(false)            // Karma export modal

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await activityLogAPI.get(date, { all_users: allUsers, source })
      setData(res.data?.data || res.data)
      setSelected(new Set())
    } catch (e) {
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [date, allUsers, source])

  useEffect(() => { load() }, [load])

  const setStatus = async (item, status) => {
    // Toggle off back to pending if the same button is clicked again.
    const next = item.status === status ? 'pending' : status
    setSaving(item.item_key)
    try {
      await activityLogAPI.validate({
        date, item_key: item.item_key, status: next,
        item_summary: item.summary, note: item.note || null,
      })
      setData(d => ({
        ...d,
        pointers: d.pointers.map(p =>
          p.item_key === item.item_key ? { ...p, status: next } : p),
      }))
    } catch (e) {
      toast.error('Failed to save validation')
    } finally {
      setSaving('')
    }
  }

  const saveNote = async (item, note) => {
    try {
      await activityLogAPI.validate({
        date, item_key: item.item_key, status: item.status,
        item_summary: item.summary, note,
      })
    } catch (e) { /* silent — non-critical */ }
  }

  const copyReport = () => {
    if (!data?.pointers?.length) return
    const lines = data.pointers.map(p => {
      const mark = p.status === 'yes' ? '[x]' : p.status === 'no' ? '[NO]' : '[ ]'
      return `- ${mark} ${p.subject} — ${p.correction} · ${p.rows} row(s), ${p.first_at}–${p.last_at}`
    })
    const header = `ARS Daily Activity — ${data.date} (${data.user_filter})`
    navigator.clipboard.writeText([header, ...lines].join('\n'))
    toast.success('Report copied to clipboard')
  }

  // ---- Row selection (for Karma generation) ----
  const pointers = data?.pointers || []
  const toggleRow = (key) => setSelected(s => {
    const n = new Set(s)
    n.has(key) ? n.delete(key) : n.add(key)
    return n
  })
  const allSelected = pointers.length > 0 && selected.size === pointers.length
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(pointers.map(p => p.item_key)))

  // Rows chosen for Karma: the checked ones, or all if nothing is checked.
  const karmaRows = () => {
    const chosen = selected.size ? pointers.filter(p => selected.has(p.item_key)) : pointers
    return chosen.map((p, i) => ({
      sno: i + 1,
      task: p.correction,
      taskType: karmaTaskType(p.subject),
      from: to12h(p.first_at),
      to: to12h(p.last_at),
      yn: p.status === 'yes' ? 'Yes' : p.status === 'no' ? 'No' : '',
      note: p.note || '',
    }))
  }

  const copyKarmaTSV = () => {
    const rows = karmaRows()
    if (!rows.length) return toast.error('Nothing to generate')
    const body = rows.map(r => [r.sno, r.task, r.taskType, r.from, r.to, r.yn, r.note].join('\t'))
    navigator.clipboard.writeText([KARMA_COLS.join('\t'), ...body].join('\n'))
    toast.success(`Copied ${rows.length} row(s) — paste into Karma / Excel`)
  }

  const downloadKarmaCSV = () => {
    const rows = karmaRows()
    if (!rows.length) return toast.error('Nothing to generate')
    const esc = v => `"${String(v ?? '').replace(/"/g, '""')}"`
    const csv = [
      KARMA_COLS.map(esc).join(','),
      ...rows.map(r => [r.sno, r.task, r.taskType, r.from, r.to, r.yn, r.note].map(esc).join(',')),
    ].join('\r\n')
    const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `karma-daily-activity-${date}.csv`
    a.click()
    URL.revokeObjectURL(url)
    toast.success('CSV downloaded')
  }

  const counts = data?.status_counts || { yes: 0, no: 0, pending: 0 }

  return (
    <div style={{ padding: '16px 20px', fontFamily: 'Inter,system-ui,sans-serif',
                  fontSize: 11, color: C.text, background: C.bg, minHeight: '100vh' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <ClipboardCheck size={16} color={C.primary} />
        <div>
          <div style={{ fontSize: 13, fontWeight: 800 }}>Daily Activity Log</div>
          <div style={{ fontSize: 10, color: C.textMuted }}>
            What you did in ARS, as daily pointers — validate each with YES / NO
          </div>
        </div>
        <div style={{ flex: 1 }} />
        <button onClick={() => setShowGen(true)} style={btn(C.primary, C.primary, '#fff')}>
          <Zap size={11} /> Generate for Karma
        </button>
        <button onClick={copyReport} style={btn(C.border, '#fff', C.textSub)}>
          <Copy size={11} /> Copy report
        </button>
        <button onClick={load} style={btn(C.border, '#fff', C.textSub)}>
          <RefreshCw size={11} /> Refresh
        </button>
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 14,
                    background: C.card, border: `1px solid ${C.border}`,
                    borderRadius: 8, padding: '10px 14px' }}>
        <label style={{ fontWeight: 600, color: C.textSub }}>Date</label>
        <input type="date" value={date} max={todayISO()}
               onChange={e => setDate(e.target.value)}
               style={{ padding: '5px 8px', border: `1px solid ${C.border}`,
                        borderRadius: 6, fontSize: 11 }} />
        <label style={{ fontWeight: 600, color: C.textSub, marginLeft: 6 }}>Source</label>
        <select value={source} onChange={e => setSource(e.target.value)}
                style={{ padding: '5px 8px', border: `1px solid ${C.border}`,
                         borderRadius: 6, fontSize: 11, background: '#fff' }}>
          <option value="both">Both (dev + app)</option>
          <option value="dev">Dev changes (code/docs)</option>
          <option value="audit">App actions (audit log)</option>
        </select>
        <button onClick={() => setAllUsers(v => !v)} style={btn(
          allUsers ? C.primary : C.border, allUsers ? '#eef2ff' : '#fff',
          allUsers ? C.primary : C.textSub)}>
          {allUsers ? <Users size={11} /> : <UserIcon size={11} />}
          {allUsers ? 'All users' : 'Only me'}
        </button>
        {pointers.length > 0 && (
          <button onClick={toggleAll} style={btn(C.border, '#fff', C.textSub)}>
            {allSelected ? <CheckSquare size={11} /> : <Square size={11} />}
            {selected.size ? `${selected.size} selected` : 'Select all'}
          </button>
        )}

        <div style={{ flex: 1 }} />
        {data && (
          <div style={{ display: 'flex', gap: 8 }}>
            <Pill label={`${data.total_pointers} pointers`} bg="#eef2ff" fg={C.primary} />
            <Pill label={`${data.total_rows} rows`} bg="#f1f5f9" fg={C.textSub} />
            <Pill label={`YES ${counts.yes}`} bg="#dcfce7" fg={C.green} />
            <Pill label={`NO ${counts.no}`} bg="#fee2e2" fg={C.red} />
            <Pill label={`Pending ${counts.pending}`} bg="#f1f5f9" fg={C.textSub} />
          </div>
        )}
      </div>

      {/* List */}
      {loading && <div style={{ color: C.textMuted, padding: 30, textAlign: 'center' }}>Loading…</div>}
      {!loading && (!data || !data.pointers?.length) && (
        <div style={{ color: C.textMuted, padding: 40, textAlign: 'center',
                      background: C.card, border: `1px dashed ${C.border}`, borderRadius: 8 }}>
          No recorded activity for {date}.
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {!loading && data?.pointers?.map(item => {
          const st = STATUS_STYLE[item.status] || STATUS_STYLE.pending
          return (
            <div key={item.item_key} style={{
              background: C.card, border: `1px solid ${C.border}`,
              borderLeft: `3px solid ${st.fg}`, borderRadius: 8, padding: '10px 14px',
              display: 'flex', alignItems: 'flex-start', gap: 12,
            }}>
              <button onClick={() => toggleRow(item.item_key)} title="Select for Karma"
                      style={{ background: 'none', border: 'none', cursor: 'pointer',
                               padding: 0, marginTop: 1, color: selected.has(item.item_key) ? C.primary : C.textMuted }}>
                {selected.has(item.item_key) ? <CheckSquare size={15} /> : <Square size={15} />}
              </button>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 3 }}>
                  <span style={{ fontWeight: 700, fontSize: 12 }}>{item.subject}</span>
                  <Pill label={item.action_type} bg="#f1f5f9" fg={C.textSub} />
                  <Pill label={st.label} bg={st.bg} fg={st.fg} />
                </div>
                <div style={{ color: C.textSub, marginBottom: 4 }}>{item.summary}</div>
                <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', color: C.textMuted, fontSize: 10 }}>
                  <span><Clock size={9} style={{ verticalAlign: -1 }} /> {item.first_at}–{item.last_at}</span>
                  {allUsers && item.users?.length > 0 && <span>by {item.users.join(', ')}</span>}
                  {item.sources?.length > 0 && <span>via {item.sources.join(', ')}</span>}
                  <span title={item.tables.join(', ')}>
                    {item.tables.length} table{item.tables.length !== 1 ? 's' : ''}
                  </span>
                </div>
                <input
                  defaultValue={item.note || ''}
                  placeholder="Add a note (optional)…"
                  onBlur={e => { if (e.target.value !== (item.note || '')) saveNote(item, e.target.value) }}
                  style={{ marginTop: 6, width: '100%', maxWidth: 460, padding: '4px 8px',
                           border: `1px solid ${C.border}`, borderRadius: 6, fontSize: 10 }}
                />
              </div>
              <div style={{ display: 'flex', gap: 6 }}>
                <button
                  disabled={saving === item.item_key}
                  onClick={() => setStatus(item, 'yes')}
                  style={btn(item.status === 'yes' ? C.green : C.border,
                             item.status === 'yes' ? '#dcfce7' : '#fff',
                             item.status === 'yes' ? C.green : C.textSub)}>
                  <Check size={12} /> Yes
                </button>
                <button
                  disabled={saving === item.item_key}
                  onClick={() => setStatus(item, 'no')}
                  style={btn(item.status === 'no' ? C.red : C.border,
                             item.status === 'no' ? '#fee2e2' : '#fff',
                             item.status === 'no' ? C.red : C.textSub)}>
                  <X size={12} /> No
                </button>
              </div>
            </div>
          )
        })}
      </div>

      {/* Karma generation modal */}
      {showGen && (
        <KarmaModal
          date={date}
          rows={karmaRows()}
          onCopy={copyKarmaTSV}
          onCsv={downloadKarmaCSV}
          onClose={() => setShowGen(false)}
        />
      )}
    </div>
  )
}

function KarmaModal({ date, rows, onCopy, onCsv, onClose }) {
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 9999,
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: C.card, borderRadius: 10, width: 'min(900px, 94vw)',
        maxHeight: '86vh', display: 'flex', flexDirection: 'column',
        boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '14px 18px',
                      borderBottom: `1px solid ${C.border}` }}>
          <Zap size={16} color={C.primary} />
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 800 }}>Karma Daily Execution — {date}</div>
            <div style={{ fontSize: 10, color: C.textMuted }}>
              {rows.length} row(s). Copy to paste into Karma / Excel, or download CSV.
            </div>
          </div>
          <button onClick={onCopy} style={btn(C.primary, C.primary, '#fff')}><Copy size={11} /> Copy</button>
          <button onClick={onCsv} style={btn(C.border, '#fff', C.textSub)}><Download size={11} /> CSV</button>
          <button onClick={onClose} style={btn(C.border, '#fff', C.textSub)}><X size={11} /></button>
        </div>

        <div style={{ overflow: 'auto', padding: '4px 0' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead>
              <tr style={{ position: 'sticky', top: 0, background: '#f8fafc' }}>
                {KARMA_COLS.map(c => (
                  <th key={c} style={{ textAlign: 'left', padding: '8px 12px', fontSize: 10,
                                       color: C.textSub, borderBottom: `1px solid ${C.border}`,
                                       whiteSpace: 'nowrap' }}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr><td colSpan={KARMA_COLS.length} style={{ padding: 24, textAlign: 'center', color: C.textMuted }}>
                  No rows selected for {date}.
                </td></tr>
              )}
              {rows.map(r => (
                <tr key={r.sno} style={{ borderBottom: `1px solid ${C.border}` }}>
                  <td style={td}>{r.sno}</td>
                  <td style={{ ...td, fontWeight: 600 }}>{r.task}</td>
                  <td style={td}>{r.taskType}</td>
                  <td style={td}>{r.from}</td>
                  <td style={td}>{r.to}</td>
                  <td style={{ ...td, fontWeight: 700,
                               color: r.yn === 'Yes' ? C.green : r.yn === 'No' ? C.red : C.textMuted }}>
                    {r.yn || '—'}
                  </td>
                  <td style={{ ...td, color: C.textSub }}>{r.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

const td = { padding: '7px 12px', whiteSpace: 'nowrap' }

function Pill({ label, bg, fg }) {
  return (
    <span style={{ background: bg, color: fg, padding: '2px 7px', borderRadius: 10,
                   fontSize: 9.5, fontWeight: 700, whiteSpace: 'nowrap' }}>
      {label}
    </span>
  )
}
