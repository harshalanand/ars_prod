import { useEffect, useState } from 'react'
import { FlaskConical, Loader2, Square, Trophy, ChevronDown, ChevronRight, Eye, Check, RefreshCw } from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API } from '@/services/api'
import { C } from '@/theme/colors'
import { DataTable, Banner, Btn, Chip, Tip, n, errText } from './Sz1Ui'
import { ACTION_HELP } from './sz1Help'
import { WH, WarehouseChips, shortLabel, SubHead } from './Sz1SessionBits'

// 1SZ ALC — wizard step 3: what-if comparison of warehouse options
// (POST /sz1/run/simulate, writes nothing) plus the candidate-line preview.
// Spec: public/docs/manual/sz1_alloc.md §D.

const MEASURES = [
  ['ALLOC_QTY', 'Pcs allocated', 'Units allocated, in whole packs.'],
  ['LINES_SERVED', 'Lines', 'Store × article lines that got stock.'],
  ['STORES_SERVED', 'Stores served', 'Stores that got at least one pack.'],
  ['SPLIT_LINES', 'Split lines', 'Store × article lines served from two warehouses.'],
  ['CROSS_QTY', 'From other RDC', 'Units shipped from a warehouse other than the store’s own.'],
]

const num = { fontVariantNumeric: 'tabular-nums', textAlign: 'right', whiteSpace: 'nowrap' }
const th = { padding: '8px 10px', fontSize: 11, fontWeight: 700, color: C.textSub, background: '#f8fafc', borderBottom: `1px solid ${C.cardBorder}`, whiteSpace: 'nowrap' }
const td = { padding: '10px', borderTop: '1px solid #f1f5f9', fontSize: 12.5, color: C.text, verticalAlign: 'middle' }

const NEUTRAL = new Set(['SPLIT_LINES', 'CROSS_QTY'])      // more is not better or worse

/** Difference to the chosen option, under the number. */
function Delta({ k, v, base }) {
  if (v == null || base == null || +v === +base) return null
  const d = +v - +base
  return <div style={{ fontSize: 10.5, fontWeight: 700, color: NEUTRAL.has(k) ? C.textMuted : d > 0 ? C.green : C.red }}>{d > 0 ? '+' : '−'}{n(Math.abs(d))}</div>
}

function FillBar({ v, tone }) {
  const p = Math.max(0, Math.min(100, +v || 0))
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'flex-end' }}>
      <div style={{ width: 70, height: 6, borderRadius: 999, background: '#e2e8f0', overflow: 'hidden' }}>
        <div style={{ width: `${p}%`, height: '100%', background: tone.fg, borderRadius: 999 }} />
      </div>
      <span style={{ ...num, fontWeight: 700, minWidth: 46 }}>{v == null ? '—' : `${(+v).toFixed(1)}%`}</span>
    </div>
  )
}

/** The variants side by side; the chosen one highlighted; "Use this" switches the option. */
function CompareTable({ variants, chosenKey, onUse }) {
  const chosen = variants.find(v => v.key === chosenKey)
  const best = Math.max(...variants.map(v => +v.totals?.ALLOC_QTY || 0))
  return (
    <div style={{ overflowX: 'auto', border: `1px solid ${C.cardBorder}`, borderRadius: 12 }}>
      <table style={{ width: '100%', borderCollapse: 'separate', borderSpacing: 0 }}>
        <thead>
          <tr>
            <th style={{ ...th, textAlign: 'left' }}>Option</th>
            {MEASURES.map(([k, label, tip]) => (
              <th key={k} style={{ ...th, textAlign: 'right' }}><Tip tip={tip}><span style={{ borderBottom: `1px dotted ${C.textMuted}`, cursor: 'help' }}>{label}</span></Tip></th>
            ))}
            <th style={{ ...th, textAlign: 'right' }}>
              <Tip tip="Units allocated ÷ the size shortage of the run’s stores."><span style={{ borderBottom: `1px dotted ${C.textMuted}`, cursor: 'help' }}>Size need met</span></Tip>
            </th>
            <th style={{ ...th, textAlign: 'right' }}><Tip tip="Seconds the server took for this option."><span style={{ cursor: 'help' }}>Took</span></Tip></th>
            <th style={th} />
          </tr>
        </thead>
        <tbody>
          {variants.map(v => {
            const t = v.totals || {}
            const on = v.key === chosenKey
            const tone = WH[v.cross_rdc === 'all' ? 'all' : 'same']
            return (
              <tr key={v.key} style={{ background: on ? tone.bg : '#fff' }}>
                <td style={{ ...td, borderLeft: `4px solid ${on ? tone.fg : 'transparent'}` }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                    <WarehouseChips cross={v.cross_rdc} protect={v.protect_other} one={v.one_source} />
                    {+t.ALLOC_QTY === best && best > 0 && variants.length > 1 && (
                      <Tip tip="Allocates the most pieces of the options compared"><Trophy size={14} color={C.amber} /></Tip>
                    )}
                  </div>
                </td>
                {MEASURES.map(([k]) => (
                  <td key={k} style={{ ...td, ...num, fontWeight: k === 'ALLOC_QTY' ? 800 : 500, fontSize: k === 'ALLOC_QTY' ? 14 : 12.5 }}>
                    {n(t[k])}{!on && chosen && <Delta k={k} v={t[k]} base={chosen.totals?.[k]} />}
                  </td>
                ))}
                <td style={{ ...td, ...num }}><FillBar v={t.FILL_PCT} tone={tone} /></td>
                <td style={{ ...td, ...num, color: C.textMuted, fontSize: 11.5 }}>{v.seconds != null ? `${Math.round(v.seconds)} s` : '—'}</td>
                <td style={{ ...td, textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {on ? (
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11.5, fontWeight: 800, color: tone.fg }}><Check size={13} /> Your choice</span>
                  ) : (
                    <Btn onClick={() => onUse(v)} tip={`Switch the run to ${shortLabel(v)}. Your stores and priority stay as they are.`}>Use this</Btn>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** Today's candidate preview (lines per store, the I_ROD effect), loaded on demand. */
function CandidateLines({ body }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const k = JSON.stringify(body)
  useEffect(() => { setData(null); setOpen(false) }, [k])
  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (!next || data) return
    setBusy(true)
    try { const { data: r } = await sz1API.runPreview(body); setData(r.data) }
    catch (e) { toast.error(errText(e, 'Could not read the candidate lines')); setOpen(false) }
    finally { setBusy(false) }
  }
  const lines = data?.per_store?.reduce((a, r) => a + (r.LINES_ || 0), 0)
  return (
    <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 12 }}>
      <Tip tip={ACTION_HELP.runPreview} block>
        <button type="button" onClick={toggle} aria-expanded={open}
          style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px', border: 'none', background: 'none', cursor: 'pointer', textAlign: 'left' }}>
          {open ? <ChevronDown size={15} color={C.textSub} /> : <ChevronRight size={15} color={C.textSub} />}
          <Eye size={14} color={C.primary} />
          <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>Candidate lines</span>
          <span style={{ fontSize: 11.5, color: C.textMuted }}>lines per store and how many only the refill rounds let in</span>
          {busy && <Loader2 size={13} className="animate-spin" style={{ marginLeft: 'auto' }} />}
          {data && <span style={{ marginLeft: 'auto' }}><Chip tone="blue">{n(lines)} lines</Chip></span>}
        </button>
      </Tip>
      {open && data && (
        <div style={{ padding: '0 12px 12px', display: 'grid', gap: 12 }}>
          <div><SubHead>Per store</SubHead><DataTable rows={data.per_store || []} maxHeight={260} exportName="1SZ_ALC_preview_per_store.csv" /></div>
          <div><SubHead>First {data.sample?.length || 0} lines</SubHead><DataTable rows={data.sample || []} maxHeight={300} exportName="1SZ_ALC_preview_sample.csv" /></div>
        </div>
      )}
    </div>
  )
}

/**
 * sim — { data, variants } of the last simulate call, or null; fresh — it matches the current
 * stores and priority; planned — the variants a new call would compare.
 */
export function PreviewStep({ sim, fresh, busy, elapsed, error, planned, chosenKey, okCount, onSimulate, onStop, onUse, candidateBody }) {
  const data = sim?.data
  const variants = data?.variants || []
  const others = variants.filter(v => v.key !== chosenKey)
  const chosen = variants.find(v => v.key === chosenKey)
  const other = others.find(v => (v.cross_rdc === 'all') !== (chosen?.cross_rdc === 'all')) || others[0]

  const perStore = (data?.per_store || []).map(r => (chosen && other ? { ...r, DELTA: (+r[chosen.key] || 0) - (+r[other.key] || 0) } : r))
  const storeCols = [['ST_CD', 'Store'], ['ST_NM', 'Name'], ['RDC', 'RDC'], ['SZ_SRT', 'Size need'],
    ...variants.map(v => [v.key, shortLabel(v)]), ...(chosen && other ? [['DELTA', `Your choice − ${shortLabel(other)}`]] : [])]

  return (
    <div style={{ display: 'grid', gap: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '9px 12px', borderRadius: 10,
                    background: C.blueBg, border: `1px solid ${C.blueBd}`, fontSize: 12.5, color: C.text }}>
        <FlaskConical size={15} color={C.blue} />
        <span><b>Preview writes nothing.</b> It runs the allocation in memory for each option on your {n(okCount)} store{okCount === 1 ? '' : 's'}.</span>
        <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 6, flexWrap: 'wrap' }}>
          {planned.map(v => <Chip key={v.key} tone={v.key === chosenKey ? 'indigo' : 'slate'}>{shortLabel(v)}</Chip>)}
        </span>
      </div>

      {error && !busy && <Banner tone="red" label="Preview failed" text={error} />}

      {busy ? (
        <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: '18px 16px', display: 'grid', gap: 10 }}>
          <style>{'@keyframes sz1sweep{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}'}</style>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Loader2 size={18} className="animate-spin" color={C.primary} />
            <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>Comparing {planned.length} options on {n(okCount)} stores…</span>
            <span style={{ marginLeft: 'auto', fontSize: 13, fontWeight: 800, color: C.primary, fontVariantNumeric: 'tabular-nums' }}>{elapsed} s</span>
          </div>
          <div style={{ height: 6, borderRadius: 999, background: '#e2e8f0', overflow: 'hidden' }}>
            <div style={{ width: '40%', height: '100%', borderRadius: 999, background: `linear-gradient(90deg, ${WH.same.fg}, ${WH.all.fg})`,
                          animation: 'sz1sweep 1.4s ease-in-out infinite' }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12, color: C.textSub }}>
            <span>About 30–120 s for 150 stores. You can go back and look at your options while it works.</span>
            <Btn icon={Square} onClick={onStop} style={{ marginLeft: 'auto' }} tip="Stop waiting for this preview. Nothing is written either way.">Stop waiting</Btn>
          </div>
        </div>
      ) : !data ? (
        <div style={{ border: `1.5px dashed ${C.cardBorder}`, borderRadius: 12, padding: '28px 18px', textAlign: 'center', display: 'grid', gap: 12, justifyItems: 'center' }}>
          <div style={{ fontSize: 14, fontWeight: 800, color: C.text }}>How much would each warehouse option allocate?</div>
          <div style={{ fontSize: 12.5, color: C.textSub, maxWidth: 560, lineHeight: 1.6 }}>
            Compare {planned.map(v => shortLabel(v)).join(' with ')} on your stores: pieces allocated, stores served, split lines and how much of the size need is met.
          </div>
          <Btn kind="primary" icon={FlaskConical} onClick={onSimulate} tip="Run every option in memory and compare. Writes nothing.">Compare options</Btn>
        </div>
      ) : (
        <>
          {!fresh && (
            <Banner tone="amber" label="Out of date" text="Your stores, priority or stock changed after this preview."
              action={<Btn icon={RefreshCw} onClick={onSimulate} tip="Run the comparison again with the current choices">Compare again</Btn>} />
          )}
          <div style={{ opacity: fresh ? 1 : 0.6 }}>
            <SubHead right={(data.skipped || []).length > 0 && <Tip tip="Codes the preview left out (not found, not listed or nothing short)"><Chip tone="amber">{n(data.skipped.length)} skipped</Chip></Tip>}>
              Warehouse options compared
            </SubHead>
            <CompareTable variants={variants} chosenKey={chosenKey} onUse={onUse} />
            {!chosen && variants.length > 0 && (
              <div style={{ fontSize: 12, color: C.amber, fontWeight: 600, marginTop: 6 }}>Your current choice is not in this comparison. Compare again, or pick one with Use this.</div>
            )}
          </div>
          <div style={{ opacity: fresh ? 1 : 0.6 }}>
            <SubHead>Per store</SubHead>
            <DataTable rows={perStore} cols={storeCols} maxHeight={320} exportName="1SZ_ALC_preview_compare_per_store.csv" empty="No store got stock in any option" />
          </div>
        </>
      )}

      <CandidateLines body={candidateBody} />
    </div>
  )
}
