import { useEffect, useMemo, useRef, useState } from 'react'
import { Bookmark, Copy, Download, Sparkles, X, FileText, Plus, Pencil, Trash2, CalendarDays } from 'lucide-react'
import toast from 'react-hot-toast'
import { releaseNotesAPI } from '@/services/api'
import { C } from '@/theme/colors'

const INTRO = "Here's what changed in ARS — recorded as it happened and compiled into a daily note, in plain language with a simple example for each change."
const AREAS = ['FA & CONS', 'Reporting', 'Allocation engine', 'Dispatch', 'Sec-cap', 'Fresh / GRT',
  'UPC Tracking', 'Release Notes', 'Data quality', 'Bug fix', 'General']

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
function fmtD(iso) {
  const m = String(iso || '').slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/)
  return m ? `${m[3]}-${MONTHS[+m[2] - 1]}-${m[1]}` : (iso || '')
}
function today() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

// **bold** → <strong>
function rich(t) {
  return String(t || '').split(/(\*\*[^*]+\*\*)/g).map((s, i) =>
    s.startsWith('**') && s.endsWith('**')
      ? <strong key={i} style={{ fontWeight: 650, color: C.text }}>{s.slice(2, -2)}</strong>
      : <span key={i}>{s}</span>)
}

// Build the plain-text share note from the selected days' entries.
function buildNote(days, byDate, { emoji = true, examples = true, intro = true } = {}) {
  const lines = []
  const multi = days.length > 1
  const pushDay = (d) => {
    const items = byDate[d] || []
    const order = [], byArea = {}
    items.forEach(it => { if (!byArea[it.area]) { byArea[it.area] = []; order.push(it.area) } byArea[it.area].push(it) })
    order.forEach(a => {
      const em = byArea[a][0].emoji
      lines.push('')
      lines.push(`${emoji && em ? em + ' ' : ''}${a}`)
      byArea[a].forEach(it => {
        const mk = emoji ? (it.marker || '✅') : '-'
        lines.push(`* ${mk} ${it.title}${it.detail ? ` — ${it.detail}` : ''}`)
        if (examples && it.example) lines.push(`   ${emoji ? '↳ ' : ''}e.g. ${it.example}`)
      })
    })
  }
  lines.push(`${emoji ? '🚀 ' : ''}ARS — V2 Retail: what we shipped${!multi && days[0] ? ` (${fmtD(days[0])})` : ''}`)
  if (intro) { lines.push(''); lines.push(INTRO) }
  days.forEach(d => { if (multi) { lines.push(''); lines.push(`${emoji ? '📅 ' : ''}${fmtD(d)}`) } pushDay(d) })
  return lines.join('\n')
}

const BLANK = { id: null, note_date: today(), area: 'FA & CONS', emoji: '🆕', marker: '✅', title: '', detail: '', example: '', tag: '' }

export default function ReleaseNotesPage() {
  const [entries, setEntries] = useState([])
  const [days, setDays] = useState([])
  const [loading, setLoading] = useState(false)
  const [active, setActive] = useState(null)
  const [form, setForm] = useState(null)        // add/edit entry
  const [saving, setSaving] = useState(false)
  const [genOpen, setGenOpen] = useState(false)
  const [selDays, setSelDays] = useState([])     // days chosen for the share note
  const [emoji, setEmoji] = useState(true)
  const [examples, setExamples] = useState(true)
  const [intro, setIntro] = useState(true)

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await releaseNotesAPI.list({ limit: 3000 })
      setEntries(data.data?.items || [])
      setDays(data.data?.days || [])
    } catch (e) { toast.error(e.response?.data?.detail || 'Failed to load release notes') }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  // group entries by day (already sorted date-desc, sort_order-asc by the API)
  const byDate = useMemo(() => {
    const m = {}
    entries.forEach(e => { (m[e.note_date] ||= []).push(e) })
    return m
  }, [entries])
  const dayList = useMemo(() => {
    const ds = days.map(d => d.note_date).filter(Boolean)
    const extra = Object.keys(byDate).filter(d => !ds.includes(d))
    return [...ds, ...extra].sort((a, b) => (a < b ? 1 : -1))
  }, [days, byDate])

  useEffect(() => {
    const obs = new IntersectionObserver(
      es => es.forEach(e => { if (e.isIntersecting) setActive(e.target.id) }),
      { rootMargin: '-12% 0px -78% 0px' })
    dayList.forEach(d => { const el = document.getElementById(`day-${d}`); if (el) obs.observe(el) })
    return () => obs.disconnect()
  }, [dayList])

  const jump = (d) => document.getElementById(`day-${d}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  const openGen = () => { setSelDays(dayList.slice(0, 1)); setGenOpen(true) }   // default: latest day
  const note = useMemo(() => buildNote(selDays, byDate, { emoji, examples, intro }), [selDays, byDate, emoji, examples, intro])
  const toggleDay = (d) => setSelDays(s => s.includes(d) ? s.filter(x => x !== d) : [...s, d])

  const copy = async () => {
    try { await navigator.clipboard.writeText(note); toast.success('Share note copied') }
    catch { toast.error('Copy failed — select the text manually') }
  }
  const download = (ext) => {
    const blob = new Blob([note], { type: 'text/plain;charset=utf-8' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
    a.download = `ARS_Release_Notes_${selDays[0] || today()}.${ext}`
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href)
  }

  const saveForm = async () => {
    if (!form.title.trim()) { toast.error('Title is required'); return }
    setSaving(true)
    try {
      const body = { note_date: form.note_date, area: form.area, emoji: form.emoji, marker: form.marker,
        title: form.title, detail: form.detail, example: form.example, tag: form.tag || form.area }
      if (form.id) await releaseNotesAPI.update(form.id, body)
      else await releaseNotesAPI.add(body)
      toast.success(form.id ? 'Change updated' : 'Change recorded'); setForm(null); load()
    } catch (e) { toast.error(e.response?.data?.detail || 'Save failed') }
    finally { setSaving(false) }
  }
  const removeEntry = async (e) => {
    if (!window.confirm(`Remove "${e.title}"?`)) return
    try { await releaseNotesAPI.remove(e.id); toast.success('Removed'); load() }
    catch (err) { toast.error(err.response?.data?.detail || 'Delete failed') }
  }

  const totalItems = entries.length
  const lbl = { fontSize: 10, fontWeight: 700, color: C.textMuted, textTransform: 'uppercase', letterSpacing: '.03em', marginBottom: 3 }

  return (
    <div className="p-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
        <div>
          <h1 className="page-title">What's New · Release Notes</h1>
          <p className="text-[12px] text-gray-500 mt-0.5 max-w-2xl">{INTRO}</p>
          <div className="flex items-center gap-2 mt-2">
            <span className="badge" style={{ background: C.primaryLt, color: C.primary, fontWeight: 700 }}>Auto-compiled daily</span>
            <span className="text-[11px] text-gray-400">{dayList.length} day(s) · {totalItems} changes recorded</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => setForm({ ...BLANK })} className="btn-secondary btn-sm flex items-center gap-1.5"><Plus size={14} /> Record a change</button>
          <button onClick={openGen} disabled={!dayList.length} className="btn-primary btn-sm flex items-center gap-1.5"><Sparkles size={14} /> Generate share note</button>
        </div>
      </div>

      <div className="flex gap-5 items-start">
        {/* Bookmark rail — one bookmark per day */}
        <aside className="hidden md:block shrink-0" style={{ width: 210, position: 'sticky', top: 12, alignSelf: 'flex-start' }}>
          <div className="card" style={{ padding: 10 }}>
            <div className="flex items-center gap-1.5 px-1 pb-2 mb-1" style={{ borderBottom: `1px solid ${C.cardBorder}` }}>
              <Bookmark size={13} color={C.primary} />
              <span className="text-[11px] font-bold uppercase tracking-wide" style={{ color: C.textSub }}>Daily notes</span>
            </div>
            <nav className="flex flex-col gap-0.5">
              {dayList.map(d => {
                const on = active === `day-${d}`
                return (
                  <button key={d} onClick={() => jump(d)} className="text-left flex items-center gap-2 rounded-md transition-colors"
                    style={{ padding: '6px 8px', fontSize: 12, fontWeight: on ? 700 : 500, color: on ? C.primary : C.textSub,
                      background: on ? C.primaryLt : 'transparent', borderLeft: `2px solid ${on ? C.primary : 'transparent'}` }}>
                    <CalendarDays size={13} />
                    <span style={{ flex: 1 }}>{fmtD(d)}</span>
                    <span style={{ fontSize: 10, color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>{(byDate[d] || []).length}</span>
                  </button>
                )
              })}
              {!dayList.length && <div className="text-[11px] text-gray-400 px-1 py-2">No entries yet.</div>}
            </nav>
          </div>
        </aside>

        {/* Daily notes */}
        <div className="flex-1 min-w-0 flex flex-col gap-5">
          {dayList.map(d => (
            <div key={d} id={`day-${d}`} style={{ scrollMarginTop: 12 }}>
              <div className="flex items-center justify-between gap-2 mb-2">
                <h2 style={{ fontSize: 15, fontWeight: 800, color: C.text, margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
                  <CalendarDays size={16} color={C.primary} /> {fmtD(d)}
                </h2>
                <button onClick={() => { setSelDays([d]); setGenOpen(true) }} className="btn-secondary btn-sm !py-1 !text-[10px] flex items-center gap-1"><Sparkles size={12} /> Share this day</button>
              </div>
              <div className="flex flex-col gap-2.5">
                {(byDate[d] || []).map(e => (
                  <div key={e.id} className="card group" style={{ padding: 14 }}>
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-start gap-2.5">
                        <span style={{ fontSize: 18, lineHeight: 1.2 }}>{e.emoji || e.marker}</span>
                        <div>
                          <div style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{e.title}</div>
                          {e.detail && <p style={{ fontSize: 13, color: C.textSub, margin: '3px 0 0', maxWidth: '70ch', lineHeight: 1.55 }}>{rich(e.detail)}</p>}
                        </div>
                      </div>
                      <div className="flex items-center gap-1.5 shrink-0">
                        <span className="badge" style={{ background: C.grayBg, color: C.textSub, fontWeight: 600, fontSize: 10 }}>{e.area}</span>
                        <button title="Edit" onClick={() => setForm({ ...BLANK, ...e, detail: e.detail || '', example: e.example || '', tag: e.tag || '' })} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><Pencil size={13} color={C.textMuted} /></button>
                        <button title="Delete" onClick={() => removeEntry(e)} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><Trash2 size={13} color={C.textMuted} /></button>
                      </div>
                    </div>
                    {e.example && (
                      <div style={{ marginTop: 10, marginLeft: 30, background: C.primaryLt, border: `1px solid ${C.primaryBd}`, borderRadius: 8, padding: '8px 11px' }}>
                        <span style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.04em', color: C.primary }}>Example</span>
                        <p style={{ fontSize: 12.5, color: C.textSub, margin: '2px 0 0', lineHeight: 1.5 }}>{e.example}</p>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
          {!dayList.length && (
            <div className="card" style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>
              {loading ? 'Loading…' : 'No changes recorded yet. Click "Record a change" to add the first one.'}
            </div>
          )}
          <div className="text-center text-[11px] text-gray-400 py-2">ARS V2 Retail · Auto Replenishment System — changelog</div>
        </div>
      </div>

      {/* Record / edit entry modal */}
      {form && (
        <div onClick={() => !saving && setForm(null)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16 }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 620, maxWidth: '95vw', maxHeight: '90vh', overflow: 'auto', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12 }}>
            <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{form.id ? 'Edit change' : 'Record a change'}</div>
              <button onClick={() => !saving && setForm(null)} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><X size={18} color={C.textMuted} /></button>
            </div>
            <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                <div style={{ flex: '0 0 130px' }}><div style={lbl}>Date</div>
                  <input type="date" value={form.note_date} onChange={e => setForm(f => ({ ...f, note_date: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} /></div>
                <div style={{ flex: '1 1 180px' }}><div style={lbl}>Area</div>
                  <input list="rn-areas" value={form.area} onChange={e => setForm(f => ({ ...f, area: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} />
                  <datalist id="rn-areas">{AREAS.map(a => <option key={a} value={a} />)}</datalist></div>
                <div style={{ flex: '0 0 80px' }}><div style={lbl}>Emoji</div>
                  <input value={form.emoji} onChange={e => setForm(f => ({ ...f, emoji: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} maxLength={4} /></div>
                <div style={{ flex: '0 0 80px' }}><div style={lbl}>Marker</div>
                  <input value={form.marker} onChange={e => setForm(f => ({ ...f, marker: e.target.value }))} className="input !py-1 !text-[12px]" style={{ width: '100%' }} maxLength={4} /></div>
              </div>
              <div><div style={lbl}>Title <span style={{ color: C.red }}>*</span></div>
                <input value={form.title} onChange={e => setForm(f => ({ ...f, title: e.target.value }))} placeholder="Short headline of the change" className="input !py-1 !text-[13px]" style={{ width: '100%' }} maxLength={300} /></div>
              <div><div style={lbl}>Detail (plain language)</div>
                <textarea value={form.detail} onChange={e => setForm(f => ({ ...f, detail: e.target.value }))} placeholder="Describe what changed, simply — no jargon." rows={3} className="input !text-[13px]" style={{ width: '100%', resize: 'vertical' }} /></div>
              <div><div style={lbl}>Simple example</div>
                <textarea value={form.example} onChange={e => setForm(f => ({ ...f, example: e.target.value }))} placeholder="A concrete, everyday example a non-technical reader would understand." rows={2} className="input !text-[13px]" style={{ width: '100%', resize: 'vertical' }} /></div>
            </div>
            <div style={{ padding: '10px 16px', borderTop: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button onClick={() => setForm(null)} disabled={saving} className="btn-secondary btn-sm">Cancel</button>
              <button onClick={saveForm} disabled={saving || !form.title.trim()} className="btn-primary btn-sm">{saving ? 'Saving…' : (form.id ? 'Update' : 'Record change')}</button>
            </div>
          </div>
        </div>
      )}

      {/* Generate share note modal */}
      {genOpen && (
        <div onClick={() => setGenOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 80, background: 'rgba(0,0,0,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16 }}>
          <div onClick={e => e.stopPropagation()} style={{ width: 880, maxWidth: '96vw', maxHeight: '88vh', display: 'flex', flexDirection: 'column', background: C.card, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
            <div style={{ padding: '12px 16px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: C.text, display: 'flex', alignItems: 'center', gap: 8 }}><Sparkles size={16} color={C.primary} /> Generate share note</div>
              <button onClick={() => setGenOpen(false)} style={{ background: 'none', border: 'none', cursor: 'pointer' }}><X size={18} color={C.textMuted} /></button>
            </div>
            <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
              <div style={{ width: 220, borderRight: `1px solid ${C.cardBorder}`, overflow: 'auto', padding: 12 }}>
                <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.04em', color: C.textMuted, marginBottom: 8 }}>Include days</div>
                <div className="flex flex-col gap-1">
                  {dayList.map(d => (
                    <label key={d} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: C.textSub, cursor: 'pointer', padding: '3px 2px' }}>
                      <input type="checkbox" checked={selDays.includes(d)} onChange={() => toggleDay(d)} />
                      <span style={{ flex: 1 }}>{fmtD(d)}</span>
                      <span style={{ fontSize: 10, color: C.textMuted }}>{(byDate[d] || []).length}</span>
                    </label>
                  ))}
                </div>
                <div style={{ display: 'flex', gap: 8, margin: '8px 0' }}>
                  <button onClick={() => setSelDays([...dayList])} className="btn-secondary btn-sm !py-1 !text-[10px]">All</button>
                  <button onClick={() => setSelDays(dayList.slice(0, 1))} className="btn-secondary btn-sm !py-1 !text-[10px]">Latest</button>
                </div>
                <div style={{ borderTop: `1px solid ${C.cardBorder}`, marginTop: 8, paddingTop: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: C.textSub, cursor: 'pointer' }}><input type="checkbox" checked={emoji} onChange={e => setEmoji(e.target.checked)} /> Emojis &amp; check-marks</label>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: C.textSub, cursor: 'pointer' }}><input type="checkbox" checked={examples} onChange={e => setExamples(e.target.checked)} /> Include examples</label>
                  <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: C.textSub, cursor: 'pointer' }}><input type="checkbox" checked={intro} onChange={e => setIntro(e.target.checked)} /> Include intro line</label>
                </div>
              </div>
              <textarea readOnly value={note} style={{ flex: 1, resize: 'none', border: 'none', outline: 'none', padding: 14, fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 12, lineHeight: 1.55, color: C.text, background: C.headerBg, whiteSpace: 'pre-wrap' }} />
            </div>
            <div style={{ padding: '10px 16px', borderTop: `1px solid ${C.cardBorder}`, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: 11, color: C.textMuted }}>{selDays.length} day(s) · {note.length} chars</span>
              <div style={{ display: 'flex', gap: 8 }}>
                <button onClick={() => download('txt')} disabled={!selDays.length} className="btn-secondary btn-sm flex items-center gap-1.5"><Download size={14} /> .txt</button>
                <button onClick={() => download('md')} disabled={!selDays.length} className="btn-secondary btn-sm flex items-center gap-1.5"><FileText size={14} /> .md</button>
                <button onClick={copy} disabled={!selDays.length} className="btn-primary btn-sm flex items-center gap-1.5"><Copy size={14} /> Copy</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
