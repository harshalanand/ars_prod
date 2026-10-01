import { useEffect, useMemo, useState } from 'react'
import { Save, RotateCcw, ChevronDown, ChevronRight, AlertTriangle } from 'lucide-react'
import toast from 'react-hot-toast'
import { b2bAPI } from '@/services/api'
import { C } from '@/theme/colors'
import B2BStepper from '@/components/b2b/B2BStepper'

const SOURCE = {
  LEGACY:  { label: 'from Streamlit', fg: C.indigo, bg: C.indigoBg,
             tip: "Copied from the tool's B2B_MBQ_SETTING so both run on the same settings" },
  DEFAULT: { label: 'default',        fg: C.gray,   bg: C.grayBg,
             tip: 'The tool never saved this one; its code default was used' },
  USER:    { label: 'changed here',   fg: C.green,  bg: C.greenBg, tip: 'Saved on this page' },
}
const WORDS = {                              // plain labels for choice values
  SHORTFALL_DESC: 'Biggest shortfall', CONT_DESC: 'Highest size share', MBQ_DESC: 'Biggest target',
  GREEDY: 'Greedy', ROUND_ROBIN: 'Round robin', PROPORTIONAL: 'Proportional', FLAT: 'Flat', NONE: 'None',
  MAX_CONSUMPTION: 'Fewest picks', BIN_ORDER: 'Bin order',
  ANY: 'Any warehouse', HOME_FIRST: 'Home first', SAME: 'Home only',
  HYBRID: 'Hybrid', REQ: 'REQ sheet', MASTER: 'Master only',
}

export default function B2BSettingsPage() {
  const [rows, setRows] = useState([])
  const [draft, setDraft] = useState({})
  const [saving, setSaving] = useState(false)
  const [openHelp, setOpenHelp] = useState({})

  const apply = list => { setRows(list); setDraft(Object.fromEntries(list.map(s => [s.key, s.value ?? s.default]))) }
  useEffect(() => {
    b2bAPI.settings().then(({ data: r }) => apply(r.data.settings))
      .catch(e => toast.error(e.response?.data?.detail || 'Could not load settings'))
  }, [])

  const changed = useMemo(() => rows.filter(s => String(draft[s.key] ?? '') !== String(s.value ?? s.default)), [rows, draft])
  const rebuild = changed.some(s => s.group === 'MBQ')

  const save = async () => {
    setSaving(true)
    try {
      const { data: r } = await b2bAPI.saveSettings(Object.fromEntries(changed.map(s => [s.key, draft[s.key]])))
      r.data.needs_rebuild ? toast(r.message, { icon: '⚠️', duration: 6000 }) : toast.success(r.message)
      apply(r.data.settings)
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not save') }
    finally { setSaving(false) }
  }

  const reset = async () => {
    try {
      const { data: r } = await b2bAPI.resetSettings(null)
      toast.success(r.message); apply(r.data.settings)
    } catch (e) { toast.error(e.response?.data?.detail || 'Could not reset') }
  }

  const applies = s => Object.entries(s.applies_when || {}).every(([k, v]) => draft[k] === v)
  const group = g => rows.filter(s => s.group === g)

  return (
    <div className="space-y-3">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <h1 className="page-title">2 · Settings</h1>
          <p className="page-subtitle">{rows.length} settings. A saved value always wins; nothing falls back to a code default without showing it.</p>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <button type="button" className="btn-secondary" onClick={reset}
            title="Set every value back to the tool's code default">
            <RotateCcw size={12} /> Reset all to defaults
          </button>
          <button type="button" className="btn-secondary" onClick={() => apply(rows)} disabled={!changed.length}>
            Discard changes
          </button>
          <button type="button" className="btn-primary" onClick={save} disabled={!changed.length || saving}>
            <Save size={12} /> Save {changed.length ? `${changed.length} change${changed.length > 1 ? 's' : ''}` : ''}
          </button>
        </div>
      </div>
      <B2BStepper current="settings" />

      {rebuild && (
        <div style={{ fontSize: 11, background: C.amberBg, border: `1px solid ${C.amberBd}`, borderRadius: 6, padding: '7px 10px' }}>
          <b>You changed a demand setting.</b> After saving, the demand table is out of date until you rebuild it in Build MBQ.
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: 10, alignItems: 'start' }}>
        {[['MBQ', 'Demand · MBQ', 'change → rebuild MBQ', C.amber, C.amberBg],
          ['ALLOC', 'Allocation', 'change → just run again', C.blue, C.blueBg]].map(([g, title, cost, fg, bg]) => (
          <div key={g} className="card">
            <div style={{ padding: '7px 10px', borderBottom: `1px solid ${C.cardBorder}`, display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: C.textSub }}>{title}</span>
              <span className="badge" style={{ color: fg, background: bg }}>{cost}</span>
            </div>
            {group(g).map(s => {
              const on = applies(s)
              const dirty = changed.includes(s)
              const warn = s.key === 'ALLOC_CROSS_RDC' && draft[s.key] === 'ANY'
              const src = SOURCE[dirty ? 'USER' : s.source] || SOURCE.DEFAULT
              return (
                <div key={s.key} style={{ borderTop: `1px solid ${C.cardBorder}`, padding: '7px 10px', opacity: on ? 1 : .5,
                                          background: warn ? C.amberBg : dirty ? C.primaryLt : undefined }}>
                  {/* Fixed badge column: each row is its own grid, so an `auto` column
                      would shift the value field by the badge's width. */}
                  <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 150px 88px', gap: 10, alignItems: 'center' }}>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontSize: 11, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 5 }}>
                        {warn && <AlertTriangle size={12} style={{ color: C.amber }} />}{s.label}
                      </div>
                      <div style={{ fontSize: 9.5, color: C.textMuted, fontFamily: 'ui-monospace, Consolas, monospace' }}>
                        {s.key}{!on && ` · only when ${Object.entries(s.applies_when).map(([k, v]) => `${k} = ${v}`).join(', ')}`}
                      </div>
                    </div>
                    <Input s={s} value={draft[s.key]} onChange={v => setDraft(d => ({ ...d, [s.key]: v }))} disabled={!on} />
                    <span className="badge" title={src.tip} style={{ color: src.fg, background: src.bg, justifySelf: 'end' }}>{src.label}</span>
                  </div>
                  <button type="button" onClick={() => setOpenHelp(o => ({ ...o, [s.key]: !o[s.key] }))}
                    style={{ border: 'none', background: 'none', padding: '3px 0 0', cursor: 'pointer', fontSize: 10,
                             color: C.textSub, display: 'flex', alignItems: 'center', gap: 3 }}
                    aria-expanded={!!openHelp[s.key]}>
                    {openHelp[s.key] ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                    What this does{s.value !== s.default ? ` · default ${WORDS[s.default] || s.default}` : ''}
                  </button>
                  {openHelp[s.key] && <div style={{ fontSize: 10.5, color: C.textSub, lineHeight: 1.5, padding: '3px 0 0 14px' }}>{s.help}</div>}
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}

function Input({ s, value, onChange, disabled }) {
  if (s.kind === 'bool') {
    const on = String(value) === '1'
    return (
      <button type="button" disabled={disabled} onClick={() => onChange(on ? '0' : '1')} aria-pressed={on}
        style={{ width: 'fit-content', padding: '3px 12px', fontSize: 10.5, fontWeight: 700, borderRadius: 999, cursor: 'pointer',
                 border: `1px solid ${on ? C.primary : C.inputBd}`, background: on ? C.primary : C.card, color: on ? '#fff' : C.textSub }}>
        {on ? 'On' : 'Off'}
      </button>
    )
  }
  if (s.kind === 'choice') {
    return (
      <select className="input" value={value ?? ''} disabled={disabled} onChange={e => onChange(e.target.value)} id={`b2b-set-${s.key}`}>
        {s.choices.map(c => <option key={c} value={c}>{WORDS[c] || c}</option>)}
      </select>
    )
  }
  return (
    <input className="input" id={`b2b-set-${s.key}`} disabled={disabled} value={value ?? ''}
      type={s.kind === 'list' ? 'text' : 'number'} min={s.min ?? undefined} step={s.kind === 'int' ? 1 : 'any'}
      onChange={e => onChange(e.target.value)} />
  )
}
