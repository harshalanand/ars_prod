import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, Search, X, Layers, PackageX, Ruler, Ban, ChevronLeft, ChevronRight, BarChart3, ListFilter, Scale } from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API, saveBlob } from '@/services/api'
import { C } from '@/theme/colors'
import { Sz1Header, Panel, Banner, Kpi, DataTable, Btn, Tip, Chip, n, when, errText } from '@/components/sz1/Sz1Ui'
import { useSz1Access } from '@/components/sz1/sz1Access'
import Sz1QualityPanel from '@/components/sz1/Sz1QualityPanel'

// 1SZ ALC — CONT Gap Report: size-applicable articles whose size has no CONT %
// anywhere in Master_CONT_SZ (not in CO, not in any store), from the latest
// Step 1 build. OPT_NO_CONT = no live size of the option has any CONT %;
// SZ_NO_CONT = this size has none while other sizes do. The MSA panel
// reconciles the gap against the complete MSA stock. Spec: manual/sz1_alloc.md §B.7.
// Rendered as the Gap tab of Prepare (embedded — see Sz1BuildPage for the props).
const EMPTY = { rdc: '', maj_cat: '', gap_type: '', only_excluded: false, art: '', sort: 'pcs', page: 1 }

export default function Sz1GapPage({ embedded = false, refreshKey = 0, onMeta, goTab }) {
  const nav = useNavigate()
  const access = useSz1Access()
  const toTab = t => (goTab ? goTab(t) : nav(access.hrefFor(t)))
  const metaCb = useRef(onMeta)
  metaCb.current = onMeta
  const [status, setStatus] = useState(null)
  const [sum, setSum] = useState(null)
  const [f, setF] = useState(EMPTY)
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [, setExporting] = useState(false)
  const [cf, setCf] = useState({})                  // column filters, applied on the server
  const [gsort, setGsort] = useState(null)          // clicked header {col, dir}, applied on the server

  const params = q => Object.fromEntries(Object.entries(q).filter(([k, v]) => v !== '' && v !== false && v != null && k !== 'page' && k !== 'sort'))
  const cfParam = c => (Object.keys(c).length ? { cf: JSON.stringify(c) } : {})
  const sortParam = o => (o ? { ob: o.col, od: o.dir } : {})
  const get = useCallback(async (q, c = {}, o = null) => {
    setBusy(true)
    try { const { data: r } = await sz1API.gapRows({ ...params(q), ...cfParam(c), ...sortParam(o), sort: q.sort, page: q.page, size: 100 }); setData(r.data) }
    catch (e) { toast.error(errText(e, 'Could not read the gap lines')) }
    finally { setBusy(false) }
  }, [])
  const load = useCallback(async () => {
    try {
      const [{ data: s }, { data: g }] = await Promise.all([sz1API.status(), sz1API.gap()])
      setStatus(s.data); setSum(g.data)
    } catch (e) { toast.error(errText(e, 'Could not load the gap report')) }
  }, [])
  useEffect(() => { load(); get(EMPTY) }, [load, get])

  const go = (patch, c = cf, o = gsort) => { const q = { ...f, ...patch }; setF(q); get(q, c, o) }
  const tableFilter = {
    filters: cf, total: data?.total, sort: gsort,
    onChange: c => { setCf(c); go({ page: 1 }, c) },
    onSort: o => { setGsort(o); go({ page: 1 }, cf, o) },
    onExport: () => doExport(),
    distinct: async (col, search, others) => {
      const { data: r } = await sz1API.distinct('gap', { ...params(f), ...cfParam(others), col, search: search || undefined })
      return r.data
    },
  }
  const doExport = async () => {
    setExporting(true)
    try { const res = await sz1API.exportGap({ ...params(f), ...cfParam(cf), ...sortParam(gsort), sort: f.sort }); toast.success(`Saved ${saveBlob(res, '1SZ_ALC_CONT_GAP.csv')}`) }
    catch (e) { toast.error(errText(e, 'Export failed')) }
    finally { setExporting(false) }
  }

  const t = Object.fromEntries((sum?.by_type || []).map(r => [r.GAP_TYPE, r]))
  const opt = t.OPT_NO_CONT || {}
  const sz = t.SZ_NO_CONT || {}
  const excl = (+opt.EXCLUDED || 0) + (+sz.EXCLUDED || 0)
  const exclQ = (+opt.EXCLUDED_FNL_Q || 0) + (+sz.EXCLUDED_FNL_Q || 0)
  const rules = status?.rules
  const pages = data ? Math.max(1, Math.ceil(data.total / 100)) : 1
  const filtered = Object.keys(params(f)).length > 0 || Object.keys(cf).length > 0
  const recon = sum?.msa_recon
  const refresh = () => { load(); get(f, cf, gsort) }
  const mountKey = useRef(refreshKey)            // a bump from the Prepare header reloads; mounting already loads
  useEffect(() => { if (refreshKey !== mountKey.current) refresh() }, [refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps
  const stats = [
    { k: 'Supply build', v: sum?.build_id ? `#${sum.build_id}` : '—', x: sum?.built_at ? when(sum.built_at) : 'not built with the gap yet' },
    { k: 'Exclusion', v: rules ? (rules.GAP_EXCLUDE ? 'On' : 'Off') : '—', x: rules ? (rules.GAP_INCL_FULL_OPT ? 'incl. A / NA options' : 'A / NA options exempt') : '', tone: rules?.GAP_EXCLUDE ? 'amber' : undefined,
      tip: 'Set in Settings → Supply selection rules → Zero-CONT gap.' },
  ]
  // the Prepare header shows these numbers
  useEffect(() => { if (status || sum) metaCb.current?.({ status, stats }) }, [status, sum]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-3" style={{ paddingBottom: 12 }}>
      {!embedded && (
        <Sz1Header current="gap" title="CONT Gap Report" icon={AlertTriangle} status={status}
          subtitle="Size-applicable articles of the MSA whose size has no CONT % anywhere in Master_CONT_SZ — not in CO, not in any store. A whole option with none (OPT_NO_CONT) or one size with none (SZ_NO_CONT). Recorded with every Supply build and kept out of SZ Base."
          onRefresh={refresh} refreshTip="Reload the gap of the latest Supply build" stats={stats} />
      )}

      {!sum?.build_id && <Banner tone="blue" label="Empty" text="No gap recorded yet. Generate Supply once; every build records its zero-CONT articles here."
        action={access.tabs.supply && <Btn icon={Layers} onClick={() => toTab('supply')} tip="Open the Supply tab">Open Supply</Btn>} />}

      {recon?.source === 'live' && sum?.build_id && (
        <Banner tone="amber" label="Rebuild"
          text={`Build #${sum.build_id} was made before the current CONT rule (CO → store average → gap). The lines below are as that build recorded them; the MSA check is worked out now. Generate Supply again (then Demand) to bring both in line.`}
          action={access.tabs.supply && <Btn icon={Layers} onClick={() => toTab('supply')} tip="Open the Supply tab">Open Supply</Btn>} />
      )}
      {recon && <MsaRecon recon={recon} />}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 10 }}>
        <Kpi k="Options with no CONT" v={n(opt.OPTIONS)} x={`${n(opt.ARTICLES)} articles · ${n(opt.FNL_Q)} pcs`} icon={PackageX} tone="red"
          tip="OPT_NO_CONT: no live size of the option has a CONT % anywhere in Master_CONT_SZ — not in the CO row and not in any store row of that MAJ_CAT + size." />
        <Kpi k="Sizes with no CONT" v={n(sz.ARTICLES)} x={`${n(sz.OPTIONS)} options · ${n(sz.FNL_Q)} pcs`} icon={Ruler} tone="amber"
          tip="SZ_NO_CONT: this size has no CONT % anywhere in Master_CONT_SZ (CO or stores), while other sizes of the option do." />
        <Kpi k="Kept out of SZ Base" v={n(excl)} x={`${n(exclQ)} pcs a rule would have admitted`} icon={Ban} tone="indigo"
          tip="Gap articles that a rule (R1–R4) would have admitted, kept out because the exclusion is on." />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 12 }}>
        <Panel title="By RDC" icon={BarChart3}><DataTable rows={sum?.by_rdc || []} maxHeight={200} empty="No gap" exportName="1SZ_gap_by_rdc.csv" /></Panel>
        <Panel title="By CONT source" icon={BarChart3} tip="NOT_IN_MASTER: the MAJ_CAT + size has no row in Master_CONT_SZ at all. ZERO_IN_MASTER: it has rows, but every CONT (CO and stores) is 0.">
          <DataTable rows={sum?.by_src || []} maxHeight={200} empty="No gap" exportName="1SZ_gap_by_cont_src.csv" />
        </Panel>
      </div>

      <Panel title="Categories with the biggest gap" icon={BarChart3} tip="Top 100 categories by gap pieces. SIZES_WITHOUT_CONT = the distinct gap sizes (both types); MASTER_SIZES = the sizes Master_CONT_SZ does give a CONT % (CO or any store) — compare the two to see what is missing.">
        <DataTable rows={sum?.by_cat || []} maxHeight={320} empty="No gap" exportName="1SZ_gap_by_category.csv" />
      </Panel>

      <Panel title="Gap lines" icon={ListFilter}
        right={<>
          {data && <Chip tone="indigo">{n(data.total)} lines · {n(data.fnl_q)} pcs</Chip>}
        </>}>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12, alignItems: 'center' }}>
          <Tip tip="Gap type"><select className="input" style={{ width: 190 }} value={f.gap_type} onChange={e => go({ gap_type: e.target.value, page: 1 })}>
            <option value="">Both gap types</option><option value="OPT_NO_CONT">Whole option, no CONT</option><option value="SZ_NO_CONT">Size, no CONT</option>
          </select></Tip>
          <Tip tip="Warehouse"><select className="input" style={{ width: 100 }} value={f.rdc} onChange={e => go({ rdc: e.target.value, page: 1 })}>
            <option value="">All RDCs</option>{['DH24', 'DW01'].map(r => <option key={r} value={r}>{r}</option>)}
          </select></Tip>
          <Tip tip="Part of a category name"><input className="input" style={{ width: 170 }} placeholder="MAJ_CAT contains" value={f.maj_cat} onChange={e => setF({ ...f, maj_cat: e.target.value.toUpperCase() })} /></Tip>
          <Tip tip="Variant or generic article"><input className="input" style={{ width: 140 }} placeholder="Article / gen art" value={f.art} onChange={e => setF({ ...f, art: e.target.value })} /></Tip>
          <Tip tip="Only the lines a rule would have admitted, kept out by the gap rule">
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5, cursor: 'pointer' }}>
              <input type="checkbox" checked={f.only_excluded} onChange={e => go({ only_excluded: e.target.checked, page: 1 })} /> Kept out only
            </label>
          </Tip>
          <Tip tip="Order"><select className="input" style={{ width: 150 }} value={f.sort} onChange={e => go({ sort: e.target.value, page: 1 })}>
            <option value="pcs">Most pieces</option><option value="cat">Category, option, size</option><option value="opt">Option, size</option>
          </select></Tip>
          <Btn icon={Search} onClick={() => go({ page: 1 })} busy={busy} tip="Apply the filters">Show</Btn>
          {filtered && <Btn icon={X} onClick={() => { setCf({}); setGsort(null); go(EMPTY, {}, null) }} tip="Clear every filter">Clear</Btn>}
          <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6 }}>
            <Btn icon={ChevronLeft} disabled={f.page <= 1} onClick={() => go({ page: f.page - 1 })} tip="Previous page" />
            <span style={{ fontSize: 12, color: C.textSub }}>page {f.page} of {pages}</span>
            <Btn icon={ChevronRight} disabled={f.page >= pages} onClick={() => go({ page: f.page + 1 })} tip="Next page" />
          </div>
        </div>
        <DataTable rows={data?.rows || []} maxHeight={500} server={tableFilter} empty="No gap line matches" />
      </Panel>

      {/* what the 1SZ rules work around in the masters — read only, for the master-data owners */}
      <Sz1QualityPanel refreshKey={refreshKey} />
    </div>
  )
}

// Where every piece of the complete MSA stock (ARS_MSA_TOTAL) went in Step 1 —
// proves the gap is measured on the whole MSA, nothing left out.
function MsaRecon({ recon }) {
  const t = recon.total || {}
  const b = recon.buckets || []
  const liveQ = +t.LIVE_FNL_Q || 0
  const sumA = b.reduce((a, r) => a + (+r.ARTICLES || 0), 0)
  const sumQ = b.reduce((a, r) => a + (+r.FNL_Q || 0), 0)
  const ok = sumA === +t.LIVE_ARTICLES && Math.abs(sumQ - liveQ) < 0.5
  const share = q => (liveQ ? `${((q / liveQ) * 100).toFixed(2)}%` : '—')
  const rowsOut = [
    { STAGE: 'All MSA articles (every RDC)', ARTICLES: +t.ARTICLES, FNL_Q: +t.FNL_Q, SHARE: '', NOTE: `stock ${n(t.STK_QTY)} pcs` },
    { STAGE: '− not free (FNL_Q ≤ 0): nothing to allocate', ARTICLES: (+t.ARTICLES || 0) - (+t.LIVE_ARTICLES || 0), FNL_Q: (+t.FNL_Q || 0) - liveQ, SHARE: '', NOTE: 'not considered' },
    { STAGE: '= Free stock (FNL_Q > 0)', ARTICLES: +t.LIVE_ARTICLES, FNL_Q: liveQ, SHARE: '100.00%', NOTE: 'split below' },
    ...b.map(r => ({ STAGE: `· ${r.BUCKET}`, ARTICLES: +r.ARTICLES, FNL_Q: +r.FNL_Q, SHARE: share(+r.FNL_Q), NOTE: `${n(r.RULE_FIRES)} admitted by a rule` })),
    { STAGE: '= Sum of the split', ARTICLES: sumA, FNL_Q: sumQ, SHARE: share(sumQ), NOTE: ok ? '✓ equals free stock' : '✗ does not reconcile' },
  ]
  return (
    <Panel title="Gap vs complete MSA stock" icon={Scale} collapseKey="recon"
      tip="The gap is measured on the WHOLE MSA (ARS_MSA_TOTAL, every RDC and article). Free stock is split into: not size-applicable, has CONT, the two gap types and A / NA. The split must add back to the free stock."
      right={<>
        <Chip tone={ok ? 'green' : 'red'}>{ok ? 'Reconciles with MSA' : 'Does not reconcile'}</Chip>
        <Chip>{recon.source === 'build' ? 'as at the build' : 'worked out now (build predates this check)'}</Chip>
      </>}>
      <DataTable rows={rowsOut} cols={[['STAGE', 'Stage'], ['ARTICLES', 'Articles'], ['FNL_Q', 'Free qty (FNL_Q)'], ['SHARE', '% of free'], ['NOTE', 'Note']]} maxHeight={320} sortable={false} exportName="1SZ_gap_vs_msa.csv" />
    </Panel>
  )
}
