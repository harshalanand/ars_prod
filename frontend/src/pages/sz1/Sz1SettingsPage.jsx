import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Settings2, SlidersHorizontal, Percent, Ruler, Cpu, Eye, X, Layers, Shield, GitBranch, Scale, Globe } from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API } from '@/services/api'
import { C } from '@/theme/colors'
import { Sz1Header, Panel, Banner, Sub, DataTable, Btn, Chip, Tip, errText } from '@/components/sz1/Sz1Ui'
import { Sz1RulesEditor } from '@/components/sz1/Sz1RulesPanel'
import { STEP_TEXT } from '@/components/sz1/sz1StepText'
import { SZ_SHARE_LADDER, SZ_SHARE_EXAMPLE } from '@/components/sz1/sz1Help'
import { useSz1Access } from '@/components/sz1/sz1Access'

// 1SZ ALC — Settings: the Supply selection rules (R1–R4 + zero-CONT gap) as a
// page, the read-only Demand size-share ladder, and the run defaults.
// Saving writes ARS_SZ1_SETTING only; a build applies the values.
// Spec: public/docs/manual/sz1_alloc.md §B–D.

const RUN_DEFAULTS = [
  { icon: Globe, name: 'Warehouses', value: 'All RDC',
    tip: 'Every warehouse’s free stock is one pool per article; stores of every RDC compete for it in priority order. A pack never ships half from two warehouses. (The other choice: Own RDC.)' },
  { icon: Shield, name: 'Protect other RDC’s stores', value: 'On',
    tip: 'A store may draw another RDC’s stock only up to that RDC’s surplus — its stock less what its own listed short stores claim. Home stock is not limited.' },
  { icon: GitBranch, name: 'One source per line', value: 'On',
    tip: 'A store’s line is filled from one warehouse: home first if it can cover the whole need, else an RDC that can. Split only when no single RDC can cover it.' },
  { icon: Scale, name: 'Priority', value: 'Balanced · 0.5',
    tip: 'Inside each category + size, stores are ordered by 0.5 × rank of the category shortage (MJ_SRT) + 0.5 × rank of the size shortage (MJ_SZ_SRT). Other choices: Biggest total shortage, Own sequence.' },
]

export default function Sz1SettingsPage() {
  const nav = useNavigate()
  const access = useSz1Access()
  const [preview, setPreview] = useState(null)
  const [previewing, setPreviewing] = useState(false)
  const [savedNow, setSavedNow] = useState(false)
  const s1 = STEP_TEXT[1]
  const s2 = STEP_TEXT[2]

  const doPreview = async values => {
    setPreviewing(true)
    try { const { data: r } = await sz1API.preview(1, values); setPreview({ ...r.data, unsaved: !!values }) }
    catch (e) { toast.error(errText(e, 'Preview failed')) }
    finally { setPreviewing(false) }
  }

  return (
    <div className="space-y-3" style={{ paddingBottom: 12 }}>
      <Sz1Header current="settings" title="Settings" icon={Settings2}
        subtitle="The rules each build follows. Supply's selection rules can be changed here; Demand's size share and the run defaults are shown for reference." />

      {savedNow && (
        <Banner tone="green" label="Saved" text="The new rules apply from the next Generate of Supply, then Demand."
          action={access.tabs.supply && <Btn kind="primary" icon={Layers} onClick={() => nav(access.hrefFor('supply'))} tip="Open Prepare → Supply">Rebuild Supply</Btn>} />
      )}

      <Panel title="Supply · selection rules" icon={SlidersHorizontal} accent={C.primary} collapseKey="rules"
        tip="Which warehouse articles become candidates (Step 1). Switch each rule on or off and set its value. Preview impact runs the selection with the form’s values without writing anything."
        right={<Chip tone="indigo">Step 1</Chip>}>
        <Sz1RulesEditor onPreview={doPreview} previewing={previewing} impact={preview?.by_rule}
          onSaved={() => setSavedNow(true)} />
      </Panel>

      {preview && (
        <Panel title="Impact preview — nothing written" icon={Eye} collapseKey="preview"
          right={<>
            {preview.unsaved && <Chip tone="amber">form values, not saved</Chip>}
            <Btn icon={X} onClick={() => setPreview(null)} tip="Close the preview" />
          </>}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 16 }}>
            {preview.by_rule && <div><Sub tip="Articles each rule admits, and the zero-CONT gap.">Per rule</Sub><DataTable rows={preview.by_rule} maxHeight={220} exportName="1SZ_rules_preview_by_rule.csv" /></div>}
            {preview.by_rdc && <div><Sub>Per RDC</Sub><DataTable rows={preview.by_rdc} maxHeight={220} exportName="1SZ_rules_preview_by_rdc.csv" /></div>}
            {preview.live && <div><Sub tip="The population the rules look at.">Live articles</Sub><DataTable rows={preview.live} maxHeight={220} exportName="1SZ_rules_preview_live.csv" /></div>}
          </div>
        </Panel>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(420px, 100%), 1fr))', gap: 12, alignItems: 'start' }}>
        <Panel title="Supply · article CONT %" icon={Percent} collapseKey="cont"
          tip="How each size gets its company CONT % in Step 1. Fixed by the spec; read only."
          right={<Chip>read only</Chip>}>
          <DataTable rows={s1.rules} maxHeight={300} toolbar={false} sortable={false} filterable={false} />
          <Note>{s1.rulesNote}</Note>
        </Panel>

        <Panel title="Demand · size share" icon={Ruler} collapseKey="share"
          tip="How each store’s category need is split over the sizes in Step 2 (Listing Level 2 ladder, scaled to 100 %). First match wins. Read only."
          right={<Chip>read only</Chip>}>
          <DataTable rows={SZ_SHARE_LADDER} maxHeight={300} toolbar={false} sortable={false} filterable={false} />
          <Note>{s2.rulesNote}</Note>
          <div style={{ marginTop: 12 }}>
            <Sub>{s2.exampleTitle}</Sub>
            <DataTable rows={SZ_SHARE_EXAMPLE} maxHeight={200} toolbar={false} sortable={false} filterable={false} />
          </div>
        </Panel>
      </div>

      <Panel title="Run defaults" icon={Cpu} collapseKey="run"
        tip="What Plan a run starts with. Change them per run on that page; they are not saved here."
        right={access.run && <Btn icon={Cpu} onClick={() => nav('/sz1-alloc/run')} tip="Open Plan a run">Plan a run</Btn>}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 10 }}>
          {RUN_DEFAULTS.map(d => {
            const Icon = d.icon
            return (
              <Tip key={d.name} tip={{ title: d.name, body: d.tip }} block>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: '10px 12px', cursor: 'help' }}>
                  <span style={{ width: 30, height: 30, borderRadius: 9, background: C.primaryLt, color: C.primary, display: 'grid', placeItems: 'center', flex: '0 0 30px' }}><Icon size={15} /></span>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 11.5, color: C.textSub, fontWeight: 600 }}>{d.name}</div>
                    <div style={{ fontSize: 14, fontWeight: 800, color: C.text }}>{d.value}</div>
                  </div>
                </div>
              </Tip>
            )
          })}
        </div>
        <Note>Each run is saved as a session with the settings it used, so a later change never alters an earlier session.</Note>
      </Panel>
    </div>
  )
}

const Note = ({ children }) => (
  <div style={{ fontSize: 12, color: C.textSub, marginTop: 10, lineHeight: 1.55 }}>{children}</div>
)
