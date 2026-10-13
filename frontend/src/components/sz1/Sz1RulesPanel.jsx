import { useCallback, useEffect, useRef, useState } from 'react'
import { SlidersHorizontal, Save, RotateCcw, Eye, History, AlertTriangle, Undo2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API } from '@/services/api'
import { C } from '@/theme/colors'
import { Modal, Btn, Chip, Tip, InfoTip, n, when, errText } from '@/components/sz1/Sz1Ui'

// Step 1 (Supply) rule settings. The same cards render in a popup (opened
// from the Supply tab's "Selection rules" label) and on the Settings page.
// An on/off switch and a value per rule, saved in ARS_SZ1_SETTING and stamped
// on every build. R2 and R4 share ONE option-qty variable (OPT_FNL_MAX).
// Spec: public/docs/manual/sz1_alloc.md §B.
const num = (v, fb) => (v === '' || v == null || isNaN(+v) ? fb : +v)

/** One-line summary of the saved rules, for the setting label. */
export function rulesSummary(s) {
  if (!s) return 'loading…'
  const r1 = s.R1_ON ? (s.R1_BASIS === 'cont' ? `R1 CONT ≤ ${s.R1_OPT_CONT_MAX}%` : `R1 sizes ≤ ${s.R1_VAR_MAX}`) : 'R1 off'
  const r2 = s.R2_ON ? (s.R2_CAP_ON ? `R2 ≤ ${s.OPT_FNL_MAX}` : 'R2 no cap') : 'R2 off'
  const r3 = s.R3_ON ? `R3 ≤ ${s.R3_CAT_FNL_MAX}` : 'R3 off'
  const r4 = s.R4_ON ? `R4 ≤ ${s.OPT_FNL_MAX}` : 'R4 off'
  const gap = s.GAP_EXCLUDE == null ? 'no gap rule' : `gap ${s.GAP_EXCLUDE ? 'excluded' : 'kept'}`
  return `${r1} · ${r2} · ${r3} · ${r4} · ${gap}`
}

/**
 * The rule form's state: the saved values, the form, save / defaults / tool
 * rules. active — load when it turns true (a popup loads when it opens).
 */
export function useSz1Rules({ active = true, onSaved } = {}) {
  const [saved, setSaved] = useState(null)
  const [form, setForm] = useState(null)
  const [meta, setMeta] = useState({})
  const [preset, setPreset] = useState(null)
  const [busy, setBusy] = useState(false)
  const savedCb = useRef(onSaved)
  savedCb.current = onSaved

  const load = useCallback(async () => {
    try {
      const { data: r } = await sz1API.settings()
      setSaved(r.data.settings); setForm(r.data.settings); setMeta(r.data.meta || {}); setPreset(r.data.tool_preset)
    } catch (e) { toast.error(errText(e, 'Could not load the rule settings')) }
  }, [])
  useEffect(() => { if (active) load() }, [active, load])

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const dirty = !!(form && saved && JSON.stringify(form) !== JSON.stringify(saved))
  const values = () => ({ ...form, R1_OPT_CONT_MAX: num(form.R1_OPT_CONT_MAX, 40), R1_VAR_MAX: num(form.R1_VAR_MAX, 2),
                          R3_CAT_FNL_MAX: num(form.R3_CAT_FNL_MAX, 200), OPT_FNL_MAX: num(form.OPT_FNL_MAX, 20) })
  const done = (s, m) => { setSaved(s); setForm(s); if (m) setMeta(m); savedCb.current?.(s) }
  const save = async () => {
    setBusy(true)
    try { const { data: r } = await sz1API.saveSettings(values()); done(r.data.settings, r.data.meta); toast.success(r.message); return true }
    catch (e) { toast.error(errText(e, 'Could not save')); return false }
    finally { setBusy(false) }
  }
  const reset = async () => {
    setBusy(true)
    try { const { data: r } = await sz1API.resetSettings(); done(r.data.settings); toast.success(r.message); return true }
    catch (e) { toast.error(errText(e, 'Could not reset')); return false }
    finally { setBusy(false) }
  }
  const loadTool = () => setForm(f => ({ ...f, ...preset }))
  const discard = () => setForm(saved)
  return { saved, form, meta, preset, busy, dirty, set, values, save, reset, loadTool, discard, reload: load }
}

/** Status chips + Discard / Defaults / Tool rules / Preview / Save — the popup footer and the page's action row. */
export function RulesActions({ r, impact, onPreview, previewing }) {
  if (!r.form) return null
  const all = (impact || []).find(x => x.RULES === 'All rules') || {}
  return (
    <>
      {r.dirty ? <Chip tone="amber">unsaved changes</Chip>
        : r.meta.updated_at ? <Chip><History size={11} style={{ verticalAlign: -1 }} /> saved {when(r.meta.updated_at)} · {r.meta.updated_by}</Chip> : <Chip>defaults</Chip>}
      {impact && <Chip tone="indigo">preview: {n(all.ARTICLES)} articles · {n(all.FNL_Q)} pcs</Chip>}
      <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {r.dirty && <Btn icon={Undo2} onClick={r.discard} disabled={r.busy} tip="Put the form back to the saved values.">Discard</Btn>}
        <Btn icon={RotateCcw} onClick={r.reset} disabled={r.busy} tip="Save the default values: R1 option CONT ≤ 40 %, R2 capped, R3 ≤ 200, R2 + R4 option ≤ 20 pcs, zero-CONT gap excluded.">Defaults</Btn>
        <Btn onClick={r.loadTool} disabled={r.busy || !r.preset}
          tip="Load the Streamlit tool's fixed rules (R1 ≤ 2 live sizes, R2 without a cap, R3 200, R4 20, no gap exclusion) into the form. Not saved until you press Save.">Tool rules</Btn>
        <Btn icon={Eye} onClick={() => onPreview(r.values())} busy={previewing} disabled={previewing || !onPreview}
          tip="Run the Supply selection with the values in the form — saved or not. Nothing is written. The impact shows on each card.">Preview impact</Btn>
        <Btn kind="primary" icon={Save} onClick={r.save} disabled={!r.dirty || r.busy} tip="Save the values. They apply from the next Generate of Supply (then Demand).">Save</Btn>
      </span>
    </>
  )
}

/** The rule cards R1–R4 + the zero-CONT gap rule. impact = preview.by_rule. */
export function RulesCards({ r, impact }) {
  const { form, set } = r
  if (!form) return <div style={{ fontSize: 12, color: C.textMuted }}>Loading the rule settings…</div>
  const imp = k => (impact || []).find(x => x.RULES === k)
  return (
    <>
      <div style={{ fontSize: 12.5, color: C.textSub, marginBottom: 12, lineHeight: 1.5 }}>
        A candidate must be in a size-applicable category (SZ_APPLICABLE = Y) and pass any enabled rule. Company CONT % comes from
        Master_CONT_SZ (stores and CO, no RDC rows): the CO row, else the average of the store rows, else 0 (gap).
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 10 }}>
        <Rule on={!!form.R1_ON} onToggle={v => set('R1_ON', v ? 1 : 0)} code="R1" name="Broken size curve" impact={imp('R1')}
          tip="The option's live sizes cover only a small part of the company size curve. ART_CONT = CO value of the article's size × 100; when CO lacks the size, the store average × 100; 0 only when no row of the master gives it a CONT. OPT_CONT = Σ ART_CONT of the option's live sizes in that RDC.">
          <Tip tip="What R1 measures: the size curve the option still covers (CONT %), or how many sizes it still has (the tool's rule).">
            <select className="input" style={{ width: 190 }} value={form.R1_BASIS} onChange={e => set('R1_BASIS', e.target.value)}>
              <option value="cont">Option CONT % (size curve)</option>
              <option value="var">Live sizes (count)</option>
            </select>
          </Tip>
          {form.R1_BASIS === 'cont'
            ? <Line>OPT_CONT ≤ <Num value={form.R1_OPT_CONT_MAX} onChange={v => set('R1_OPT_CONT_MAX', v)} max={100} step={5} /> % · size not A / NA</Line>
            : <Line>VAR_COUNT ≤ <Num value={form.R1_VAR_MAX} onChange={v => set('R1_VAR_MAX', v)} max={50} step={1} /> sizes · size not A / NA</Line>}
        </Rule>

        <Rule on={!!form.R2_ON} onToggle={v => set('R2_ON', v ? 1 : 0)} code="R2" name="No-size option" impact={imp('R2')}
          tip="An option with a single A / NA (no-size) article, allocated as a whole option. With the cap on, only options up to the option-qty value — the SAME value R4 uses.">
          <Line>single A / NA size</Line>
          <Line>
            <Tip tip="Limit R2 to options holding at most the option-qty value.">
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                <input type="checkbox" checked={!!form.R2_CAP_ON} onChange={e => set('R2_CAP_ON', e.target.checked ? 1 : 0)} /> cap: OPT_FNL ≤
              </label>
            </Tip>
            <Num value={form.OPT_FNL_MAX} onChange={v => set('OPT_FNL_MAX', v)} step={5} shared disabled={!form.R2_CAP_ON} /> pcs
          </Line>
        </Rule>

        <Rule on={!!form.R3_ON} onToggle={v => set('R3_ON', v ? 1 : 0)} code="R3" name="Low-stock category" impact={imp('R3')}
          tip="The whole category holds at most this many free pieces in the RDC — clear it as a block.">
          <Line>CAT_FNL ≤ <Num value={form.R3_CAT_FNL_MAX} onChange={v => set('R3_CAT_FNL_MAX', v)} step={10} /> pcs in the RDC</Line>
        </Rule>

        <Rule on={!!form.R4_ON} onToggle={v => set('R4_ON', v ? 1 : 0)} code="R4" name="Near-empty option" impact={imp('R4')}
          tip="The whole option holds at most this many free pieces in the RDC. The value is shared with R2's cap.">
          <Line>OPT_FNL ≤ <Num value={form.OPT_FNL_MAX} onChange={v => set('OPT_FNL_MAX', v)} step={5} shared /> pcs</Line>
        </Rule>

        <Rule on={!!form.GAP_EXCLUDE} onToggle={v => set('GAP_EXCLUDE', v ? 1 : 0)} code="GAP" name="Zero-CONT gap" impact={imp('Gap excluded')}
          impactWord="excluded" tone="amber"
          tip="An article whose size has no CONT % anywhere in Master_CONT_SZ (SZ_NO_CONT), or none of whose option's sizes has any (OPT_NO_CONT), is recorded in the CONT Gap Report and — with this on — kept out of SZ Base. Off: still reported, but candidates as before.">
          <Line>keep 0-CONT sizes and options out of SZ Base</Line>
          <Line>
            <Tip tip="Also remove no-size (A / NA) options that have no CONT.">
              <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                <input type="checkbox" checked={!!form.GAP_INCL_FULL_OPT} onChange={e => set('GAP_INCL_FULL_OPT', e.target.checked ? 1 : 0)} />
                apply to A / NA options too
              </label>
            </Tip>
            <InfoTip tip="No-size options carry 0 in the CO size curve by design, so they are exempt by default. Turning this on removes almost every R2 candidate." />
          </Line>
        </Rule>
      </div>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 10, fontSize: 11.5, color: C.textMuted }}>
        <AlertTriangle size={12} /> R2 and R4 share one option-qty value (highlighted). Saved values apply from the next Generate of Supply, then Demand.
      </div>
    </>
  )
}

/** The rules in a popup (opened from the Supply tab's "Selection rules" label). */
export default function Sz1RulesDialog({ open, onClose, onPreview, previewing, impact, onSaved }) {
  const r = useSz1Rules({ active: open, onSaved })
  return (
    <Modal open={open} onClose={onClose} title="Selection rules · Supply" icon={SlidersHorizontal} width={1180}
      footer={r.form && <RulesActions r={r} impact={impact} onPreview={onPreview} previewing={previewing} />}>
      <RulesCards r={r} impact={impact} />
    </Modal>
  )
}

/** The same rules on a page (Settings): the cards, then the action row. */
export function Sz1RulesEditor({ onPreview, previewing, impact, onSaved }) {
  const r = useSz1Rules({ onSaved })
  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <div><RulesCards r={r} impact={impact} /></div>
      {r.form && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', borderTop: `1px solid ${C.cardBorder}`, paddingTop: 12 }}>
          <RulesActions r={r} impact={impact} onPreview={onPreview} previewing={previewing} />
        </div>
      )}
    </div>
  )
}

function Rule({ on, onToggle, code, name, tip, impact, impactWord, tone, children }) {
  const accent = tone === 'amber' ? C.amber : C.primary
  return (
    <div style={{ border: `1.5px solid ${on ? accent : C.cardBorder}`, background: on ? '#fbfbff' : '#f8fafc', borderRadius: 12,
                  padding: '10px 12px', opacity: on ? 1 : 0.7, display: 'grid', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Tip tip={on ? `Turn ${code} off` : `Turn ${code} on`}>
          <button type="button" role="switch" aria-checked={on} onClick={() => onToggle(!on)}
            style={{ width: 34, height: 19, borderRadius: 999, border: 'none', cursor: 'pointer', position: 'relative',
                     background: on ? accent : '#cbd5e1', transition: 'background .15s' }}>
            <span style={{ position: 'absolute', top: 2, left: on ? 17 : 2, width: 15, height: 15, borderRadius: '50%', background: '#fff', transition: 'left .15s' }} />
          </button>
        </Tip>
        <span style={{ fontSize: 11, fontWeight: 800, color: '#fff', background: on ? accent : C.textMuted, borderRadius: 6, padding: '1px 7px' }}>{code}</span>
        <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{name}</span>
        <InfoTip tip={{ title: `${code} · ${name}`, body: tip }} />
        {impact && <span style={{ marginLeft: 'auto' }}><Chip tone={on ? (tone === 'amber' ? 'amber' : 'green') : 'slate'}>{n(impact.ARTICLES)} art · {n(impact.FNL_Q)} pcs{impactWord ? ` ${impactWord}` : ''}</Chip></span>}
      </div>
      <div style={{ display: 'grid', gap: 6, pointerEvents: on ? 'auto' : 'none' }}>{children}</div>
    </div>
  )
}

const Line = ({ children }) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5, color: C.text, flexWrap: 'wrap' }}>{children}</div>
)

function Num({ value, onChange, max, step = 1, shared, disabled }) {
  return (
    <Tip tip={shared ? 'Shared by R2 (cap) and R4 — changing it here changes both.' : undefined}>
      <input className="input" type="number" min={0} max={max} step={step} value={value ?? ''} disabled={disabled}
        onChange={e => onChange(e.target.value)}
        style={{ width: 84, height: 30, fontWeight: 700, textAlign: 'right', borderColor: shared ? C.indigoBd : undefined,
                 background: shared ? C.indigoBg : undefined }} />
    </Tip>
  )
}
