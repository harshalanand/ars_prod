import { useCallback, useEffect, useRef, useState } from 'react'
import { ShieldAlert, Percent, Sigma, Building2, Copy, Grid3x3, CircleSlash, RefreshCw, ChevronLeft, ChevronRight,
  Stethoscope, Wrench, Lightbulb, Store, Tags } from 'lucide-react'
import toast from 'react-hot-toast'
import { sz1API, saveBlob } from '@/services/api'
import { C } from '@/theme/colors'
import { Panel, Kpi, DataTable, Btn, Tip, Chip, Sub, Skeleton, Banner, n, when, errText } from './Sz1Ui'

// 1SZ ALC — Master data quality (read only). What Step 2's size share and the
// grid join currently work around, so the owners of Master_CONT_SZ and
// ARS_GRID_MJ can fix it at the source. Server: sz1_quality_service.py —
// worked out together and kept 5 minutes; tables filter / sort / page / export
// on the server from that copy. Spec: manual/sz1_alloc.md §C.

const ICONS = { A: Percent, B: Sigma, C: Building2, D: Copy, E: Grid3x3, F: CircleSlash }
const TONE = { A: 'amber', B: 'amber', C: 'red', D: 'red', E: 'red', F: 'red' }
const PAGE = 200

// what each column means (hover a chip above the table)
const COL_TIPS = {
  ST_CD: 'Store code (Master_CONT_SZ.ST_CD).', ST_NM: 'Store name from the store master.',
  RDC: "The store's home warehouse (store master).", LISTED: 'Y = LISTING = 1 in Master_ALC_INPUT_ST_MASTER, so 1SZ serves it.',
  SUB_DIV: 'Sub-division of the category (Master_CONT_SZ).', MAJ_CAT: 'Category.',
  IN_1SZ: 'Y = the category is in the latest Step 1 SZ Base, so 1SZ uses this curve today. Fix these first.',
  SZ_APPLICABLE: 'From ARS_GRID_HIERARCHY. N = sizes are split evenly; the curve is not used.',
  ISSUE: 'What is wrong with the curve.',
  REAL_SIZES: 'Real sizes (not A / NA) with CONT > 0.',
  REAL_TOTAL: 'Sum of the real sizes\' CONT (should be 1.0).',
  REAL_TOTAL_PCT: 'Sum of the real sizes\' CONT × 100 (should be 100).',
  A_CONT: 'CONT of the size A row (whole option).', NA_CONT: 'CONT of the size NA row (whole option).',
  OLD_SHARE_PCT: 'Share of the curve the real sizes got under the old rule (A / NA in the divisor). 50 = every size halved.',
  DIRECTION: 'Over or under 100 %.',
  OVERLAP: 'Y = a combined size such as 14-15Y sits next to 14Y or 15Y of the same curve — the same children counted twice.',
  OVERLAP_SIZES: 'The combined size and the single sizes it overlaps.',
  SIZES: 'Every size with CONT > 0 and its value, in size order.',
  SZ: 'Size.', DUP_ROWS: 'Rows that share the key.', MIN_CONT: 'Smallest CONT of the duplicates.',
  USED_CONT: 'The value the 1SZ SQL uses (MAX).', RAW_ROWS: 'The rows exactly as stored.',
  STOCKED_IN: 'RDC(s) where Step 1 holds the category.', SZ_BASE_FNL_Q: 'Free pieces of the category in Step 1 (all RDCs).',
  OWN_RDC_IN_MSA: "Y = the store's own RDC has the category in ARS_MSA_TOTAL.",
  STORE_HAS_STOCK_ROWS: 'Y = the store has ARS_GRID_MJ_VAR_ART rows (stock) for the category.',
  STEP2_BUILD: 'What the latest Step 2 build saw for this pair.',
  STEP2_MJ_DISP: 'Display quantity the grid gave in that Step 2 build.', STEP2_MJ_MBQ: 'MBQ the grid gave in that Step 2 build.',
  STEP2_SIZES: 'The real sizes Step 2 builds for the category.', MJ_MBQ: 'Category MBQ in Step 2.', MJ_SRT: 'Category shortage in Step 2.',
  STEP2_CONT_SRC: 'How the latest Step 2 build shared the sizes.',
  STORE_MASTER_ROWS: "The store's Master_CONT_SZ rows for the category (any value).",
  CO_MASTER_ROWS: 'The CO rows for the category (any value).',
}

export default function Sz1QualityPanel({ refreshKey = 0 }) {
  const [sum, setSum] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const load = useCallback(async (refresh = false) => {
    setBusy(true); setErr(null)
    try { const { data: r } = await sz1API.quality(refresh); setSum(r.data) }
    catch (e) { setErr(errText(e, 'Could not read the data quality report')) }
    finally { setBusy(false) }
  }, [])
  const mountKey = useRef(refreshKey)
  useEffect(() => { if (refreshKey !== mountKey.current && sum) load() }, [refreshKey]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Panel title="Master data quality" icon={ShieldAlert} collapseKey="quality"
      tip="Read only. What the 1SZ rules currently work around in Master_CONT_SZ and ARS_GRID_MJ — for the master-data owners to fix at the source. Worked out from the live tables and kept for 5 minutes."
      right={<>
        {sum && <Tip tip={`Checked ${when(sum.checked_at)} in ${sum.seconds}s · ${n(sum.master_rows)} Master_CONT_SZ rows read. Kept for 5 minutes; Re-check reads again.`}>
          <Chip>checked {when(sum.checked_at)}</Chip>
        </Tip>}
        <Btn icon={RefreshCw} busy={busy} onClick={() => load(true)} tip="Read the masters again now (about 10 s)">Re-check</Btn>
      </>}>
      <QualityBody sum={sum} busy={busy} err={err} load={load} />
    </Panel>
  )
}

// rendered only while the panel is open, so a folded panel reads nothing
function QualityBody({ sum, busy, err, load }) {
  const [sel, setSel] = useState(null)
  useEffect(() => { if (!sum && !busy) load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {                                   // first finding with something to fix
    if (sum && (!sel || !sum.items.some(i => i.key === sel))) setSel((sum.items.find(i => i.count > 0) || sum.items[0])?.key)
  }, [sum]) // eslint-disable-line react-hooks/exhaustive-deps

  if (err && !sum) return <Banner tone="red" label="Error" text={err} action={<Btn icon={RefreshCw} onClick={() => load()} tip="Try again">Retry</Btn>} />
  if (!sum) {
    return (
      <div style={{ display: 'grid', gap: 10 }}>
        <div style={{ fontSize: 12, color: C.textSub }}>Reading Master_CONT_SZ, the grid and Step 2 (about 10 seconds)…</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(190px, 100%), 1fr))', gap: 10 }}>
          {[0, 1, 2, 3, 4, 5].map(i => <Skeleton key={i} h={62} r={12} />)}
        </div>
      </div>
    )
  }
  const item = sum.items.find(i => i.key === sel)
  return (
    <div style={{ display: 'grid', gap: 12, minWidth: 0 }}>
      <div style={{ fontSize: 12, color: C.textSub, lineHeight: 1.5 }}>
        Step 2 scales every curve to 100 %, leaves A / NA out of the divisor and gives a store with no grid row a budget of 0.
        The numbers come out right, but the masters stay wrong. Click a tile to see its list; every list filters, sorts and exports in full.
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(190px, 100%), 1fr))', gap: 10 }}>
        {sum.items.map(it => {
          const on = it.key === sel
          return (
            <button key={it.key} type="button" onClick={() => setSel(it.key)} aria-pressed={on}
              style={{ all: 'unset', display: 'block', cursor: 'pointer', borderRadius: 13, minWidth: 0,
                       outline: on ? `2px solid ${C.primary}` : 'none', outlineOffset: 1 }}>
              <Kpi k={`${it.key} · ${it.title}`} v={n(it.count)} x={it.x} icon={ICONS[it.key]}
                tone={it.count > 0 ? TONE[it.key] : 'green'}
                tip={{ title: it.title, body: `${it.why} Click to see the list.` }} />
            </button>
          )
        })}
      </div>
      {item && <Finding key={item.key} item={item} checkedAt={sum.checked_at} step2={sum.step2_build} />}
    </div>
  )
}

function Finding({ item, checkedAt, step2 }) {
  const [data, setData] = useState({ rows: item.sample_rows || [], total: item.count })
  const [cf, setCf] = useState({})
  const [sort, setSort] = useState(null)
  const [page, setPage] = useState(1)
  const [busy, setBusy] = useState(false)
  const first = useRef(true)
  const cfParam = c => (Object.keys(c).length ? { cf: JSON.stringify(c) } : {})
  const sortParam = o => (o ? { ob: o.col, od: o.dir } : {})

  useEffect(() => {
    if (first.current) { first.current = false; return }      // the summary brought page 1
    let live = true
    setBusy(true)
    sz1API.qualityRows(item.key, { ...cfParam(cf), ...sortParam(sort), page, size: PAGE })
      .then(({ data: r }) => { if (live) setData(r.data) })
      .catch(e => toast.error(errText(e, 'Could not read the list')))
      .finally(() => live && setBusy(false))
    return () => { live = false }
  }, [cf, sort, page, checkedAt]) // eslint-disable-line react-hooks/exhaustive-deps

  const pages = Math.max(1, Math.ceil((data.total || 0) / PAGE))
  const server = {
    filters: cf, total: data.total, sort,
    onChange: c => { setCf(c); setPage(1) },
    onSort: o => { setSort(o); setPage(1) },
    onExport: async () => {
      try { const res = await sz1API.exportQuality(item.key, { ...cfParam(cf), ...sortParam(sort) }); toast.success(`Saved ${saveBlob(res, `1SZ_QUALITY_${item.key}.csv`)}`) }
      catch (e) { toast.error(errText(e, 'Export failed')) }
    },
    distinct: async (col, search, others) => {
      const { data: r } = await sz1API.qualityDistinct(item.key, { col, search: search || undefined, ...cfParam(others) })
      return r.data
    },
  }
  const cols = item.cols || Object.keys(item.sample_rows?.[0] || {})

  return (
    <div style={{ display: 'grid', gap: 12, minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 14, fontWeight: 700, color: C.text }}>{item.key} · {item.title}</span>
        <Chip tone={item.count ? TONE[item.key] : 'green'}>{n(item.count)} {item.count === 1 ? 'row' : 'rows'}</Chip>
        {item.units != null && item.unit_label && <Chip>{n(item.units)} {item.unit_label}</Chip>}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(280px, 100%), 1fr))', gap: 10 }}>
        <Note icon={Lightbulb} tone="amber" label="Why it matters" text={item.why} />
        <Note icon={Wrench} tone="indigo" label="How to fix" text={item.fix} />
      </div>

      {item.key === 'E' && <GridDiagnosis item={item} step2={step2} />}

      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 11.5, color: C.textSub, fontWeight: 600 }}>Columns</span>
        {cols.map(c => (
          <Tip key={c} tip={COL_TIPS[c] ? { title: c, body: COL_TIPS[c] } : null}>
            <span style={{ fontSize: 11, color: C.textSub, background: '#f1f5f9', borderRadius: 6, padding: '2px 7px', cursor: COL_TIPS[c] ? 'help' : 'default', whiteSpace: 'nowrap' }}>
              {c.replace(/_/g, ' ')}
            </span>
          </Tip>
        ))}
      </div>

      <div style={{ minWidth: 0 }}>
        <DataTable rows={data.rows || []} cols={cols} maxHeight={460} server={server}
          empty={item.count ? 'No row matches' : 'Nothing to fix — this check is clean.'} />
        {data.total > PAGE && (
          <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 6, marginTop: 8 }}>
            {busy && <span style={{ fontSize: 11.5, color: C.textMuted }}>loading…</span>}
            <Btn icon={ChevronLeft} disabled={page <= 1 || busy} onClick={() => setPage(p => p - 1)} tip="Previous page" />
            <span style={{ fontSize: 12, color: C.textSub }}>page {page} of {pages}</span>
            <Btn icon={ChevronRight} disabled={page >= pages || busy} onClick={() => setPage(p => p + 1)} tip="Next page" />
          </div>
        )}
      </div>
    </div>
  )
}

function Note({ icon: Icon, tone, label, text }) {
  const [fg, bg, bd] = tone === 'amber' ? [C.amber, C.amberBg, C.amberBd] : [C.primary, C.primaryLt, C.cardBorder]
  return (
    <div style={{ display: 'flex', gap: 9, alignItems: 'flex-start', background: bg, border: `1px solid ${bd || C.cardBorder}`, borderRadius: 10, padding: '9px 11px', minWidth: 0 }}>
      <Icon size={15} style={{ color: fg, flex: '0 0 15px', marginTop: 2 }} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 11.5, fontWeight: 700, color: fg, marginBottom: 2 }}>{label}</div>
        <div style={{ fontSize: 12.5, color: C.text, lineHeight: 1.5 }}>{text}</div>
      </div>
    </div>
  )
}

// E: why the grid has no row — the diagnosis and the two groupings
function GridDiagnosis({ item, step2 }) {
  return (
    <div style={{ display: 'grid', gap: 12, minWidth: 0 }}>
      <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 10, padding: '10px 12px', background: '#f8fafc' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12, fontWeight: 700, color: C.text, marginBottom: 6 }}>
          <Stethoscope size={14} style={{ color: C.primary }} /> Diagnosis
          {step2?.BUILD_ID && <Chip>vs Step 2 build #{step2.BUILD_ID}</Chip>}
        </div>
        <ul style={{ margin: 0, paddingLeft: 18, display: 'grid', gap: 4 }}>
          {(item.diagnosis || []).map((d, i) => <li key={i} style={{ fontSize: 12.5, color: C.text, lineHeight: 1.5 }}>{d}</li>)}
        </ul>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(340px, 100%), 1fr))', gap: 12, minWidth: 0 }}>
        <div style={{ minWidth: 0 }}>
          <Sub tip="Each category with listed stores that have no grid row for it. DH24 / DW01 STORES = by the store's home RDC; NEW SINCE STEP2 = had a row in the latest Step 2 build, none now; SHARE OF LISTED = % of all listed stores."><Tags size={13} /> By category</Sub>
          <DataTable rows={item.by_cat || []} maxHeight={280} empty="Every category is covered" exportName="1SZ_quality_grid_by_category.csv"
            cols={[['MAJ_CAT', 'MAJ_CAT'], ['STOCKED_IN', 'Stocked in'], ['MISSING_STORES', 'Stores'], ['DH24_STORES', 'DH24'], ['DW01_STORES', 'DW01'],
                   ['NEW_SINCE_STEP2', 'New since Step 2'], ['SHARE_OF_LISTED_PCT', '% of listed'], ['SZ_BASE_FNL_Q', 'Free pcs']]} />
        </div>
        <div style={{ minWidth: 0 }}>
          <Sub tip="Each listed store with categories it has no grid row for, and which ones."><Store size={13} /> By store</Sub>
          <DataTable rows={item.by_store || []} maxHeight={280} empty="Every store is covered" exportName="1SZ_quality_grid_by_store.csv"
            cols={[['RDC', 'RDC'], ['ST_CD', 'ST_CD'], ['ST_NM', 'Store'], ['MISSING_CATS', 'Categories'], ['CATS', 'Which']]} />
        </div>
      </div>
    </div>
  )
}
