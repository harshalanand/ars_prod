/**
 * OneSizePage
 * Post-MSA filter that keeps (MAJCAT, GEN_ART, CLR) combos appearing N times
 * or fewer and cross-joins them with every store in Master_ALC_INPUT_ST_MASTER.
 *
 * Read-only — no DB writes. Result is previewed in-line and exported as CSV.
 */
import { useEffect, useMemo, useState } from 'react'
import { oneSizeAPI } from '@/services/api'
import toast from 'react-hot-toast'
import { Play, Download, Filter, RefreshCw, CheckCircle2, AlertCircle, Database, Layers, Store, X } from 'lucide-react'
import { C } from '@/theme/colors'

const STAGE_LABELS = {
  latest_sequence:      'Locating latest MSA sequence',
  load_msa:             'Loading MSA rows',
  applicable_majcats:   'Reading MASTER_SZ_APPLICABLE (Y MAJCATs)',
  tag_applicable_sz:    'Tagging each row with applicable_sz = Y/N',
  group_and_count:      'Counting MAJ_CAT × GEN_ART × CLR combinations → count',
  group_fnl_q_sum:      'Summing FNL_Q per MAJ_CAT × GEN_ART × CLR → fnl_q_sum (maj_gen_cl_q)',
  group_maj_cat_q:      'Summing FNL_Q per MAJ_CAT → maj_cat_q',
  drop_applicable_sz_n: "Dropping applicable_sz='N' rows BEFORE cross-join (gate)",
  compute_row_or:       'Pre-computing row_or = (count≤2 OR maj_gen_cl_q≤50 OR maj_cat_q≤400)',
  drop_row_or_false:    "Dropping row_or=FALSE rows BEFORE cross-join — every survivor is final_msa='Y'",
  filter_placeholder:   'Dropping placeholder rows (CLR/SZ = "A")',
  recompute_aggregates_post_filter: 'Recomputing count / fnl_q_sum / maj_cat_q on filtered data (match CSV totals)',
  load_stores:          'Loading stores + ST_STATUS (display only) from Master_ALC_INPUT_ST_MASTER',
  apply_rdc_filter:     'Applying RDC selection to MSA and stores',
  cross_join:           'Joining (filtered) MSA rows with stores on RDC',
  tag_final_msa_y:      "Tagging final_msa='Y' on all surviving rows (no filter — applied pre-join)",
  enrich_cont:          'Looking up `cont` from Master_CONT_SZ (ST_CD × MAJ_CAT × SZ)',
  enrich_calc_maj_cat:  'Looking up DISP_Q + SAL_PD + ALC_D from ARS_CALC_ST_MAJ_CAT (ST_CD × MAJ_CAT)',
  enrich_sal_pd_option_grain: 'Overwriting SAL_PD with option-grain value from MASTER_GEN_ART_SALE',
  enrich_grid_mj_base:  'Looking up ACS_D from ARS_GRID_MJ (ST_CD × MAJ_CAT)',
  compute_var_art_disp: 'Computing var_art_disp = ACS_D × cont',
  compute_mbq_sz:       'Computing MBQ_SZ = (DISP_Q + SAL_PD × days) × cont',
  enrich_grid_mj:       'Looking up STK_TTL from ARS_GRID_MJ_VAR_ART (ST_CD × ARTICLE_NUMBER)',
  enrich_grid_mj_sz:    'Looking up STK_TTL_SZ from ARS_GRID_MJ_VAR_ART (ST_CD × MAJ_CAT × SZ)',
  compute_req:          'Computing REQ = MAX(MBQ_SZ − STK_TTL_SZ, 0)',
  enrich_listing:       'Computing AUTO=SAL_PD×cont + L7 + PER_OPT, then MAX_DAILY_SALE: AGE<15 → MAX(3 inputs); AGE≥15 → MAX(L7, AUTO) − PER_OPT (≥ 0)',
  compute_sale_var_art: 'Computing SALE_VAR_ART = MAX_DAILY_SALE × cont',
  compute_mbq_var:      'Computing MBQ_VAR = (MAX_DAILY_SALE × days) + (ACS_D × cont)',
  compute_var_req:      'Computing VAR_REQ = MAX(MBQ_VAR − STK_TTL, 0)',
  enrich_msa_remain_and_rank: 'Loading MSA_REMAIN pool + store ranks (ARS_STORE_RANKING)',
  drop_zero_demand:     'Dropping rows where REQ=0 OR VAR_REQ=0 (zero-demand candidates) BEFORE allocation',
  compute_allocation:   'Allocating MIN(VAR_REQ, REQ, MSA_REMAIN) — strict: BOTH REQ>0 AND VAR_REQ>0 required',
  drop_alloc_zero:      'Dropping ALLOC=0 rows (POOL_EMPTY) — final output has only rows that received stock',
}

function StageProgress({ stages, running }) {
  if (!stages?.length && !running) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {Object.keys(STAGE_LABELS).map((key) => {
        const done = stages?.find((s) => s.stage === key)
        const label = STAGE_LABELS[key]
        const detail = done
          ? Object.entries(done)
              .filter(([k]) => k !== 'stage')
              .map(([k, v]) => `${k}=${v}`)
              .join(', ')
          : ''
        return (
          <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
            {done ? (
              <CheckCircle2 size={14} color={C.green} />
            ) : running ? (
              <RefreshCw size={14} color={C.primary} className="spin" />
            ) : (
              <div style={{ width: 14, height: 14, borderRadius: 7, border: `2px solid ${C.cardBorder}` }} />
            )}
            <span style={{ color: done ? C.text : C.textMuted }}>{label}</span>
            {detail && <span style={{ color: C.textMuted, fontSize: 11 }}>— {detail}</span>}
          </div>
        )
      })}
      <style>{`.spin { animation: spin 0.9s linear infinite } @keyframes spin { to { transform: rotate(360deg) } }`}</style>
    </div>
  )
}

function MultiSelect({ options, value, onChange, disabled, allLabel = 'All', emptyHint = 'Leave empty for all' }) {
  const [open, setOpen] = useState(false)
  const selected = new Set(value)

  const toggle = (item) => {
    const next = new Set(selected)
    if (next.has(item)) next.delete(item)
    else next.add(item)
    onChange([...next])
  }

  const summary =
    value.length === 0
      ? allLabel
      : value.length <= 3
      ? value.join(', ')
      : `${value.length} selected`

  return (
    <div style={{ position: 'relative' }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={disabled || options.length === 0}
        style={{
          minWidth: 200, padding: '8px 10px', border: `1px solid ${C.inputBorder}`,
          borderRadius: 8, fontSize: 13, background: C.inputBg, color: C.text,
          textAlign: 'left', cursor: disabled || options.length === 0 ? 'not-allowed' : 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
        }}
      >
        <span style={{ color: value.length === 0 ? C.textMuted : C.text }}>{summary}</span>
        {value.length > 0 && (
          <X
            size={14}
            color={C.textMuted}
            onClick={(e) => { e.stopPropagation(); onChange([]) }}
            style={{ cursor: 'pointer' }}
          />
        )}
      </button>
      {open && options.length > 0 && (
        <>
          <div
            onClick={() => setOpen(false)}
            style={{ position: 'fixed', inset: 0, zIndex: 10 }}
          />
          <div style={{
            position: 'absolute', top: '100%', left: 0, marginTop: 4,
            minWidth: 220, maxHeight: 280, overflowY: 'auto',
            background: C.cardBg, border: `1px solid ${C.cardBorder}`,
            borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,0.12)', zIndex: 11,
            padding: 6,
          }}>
            <div style={{
              fontSize: 11, color: C.textMuted, padding: '4px 8px 6px',
              borderBottom: `1px solid ${C.cardBorder}`, marginBottom: 4,
            }}>
              {emptyHint} ({options.length} available)
            </div>
            {options.map((item) => (
              <label
                key={item}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8,
                  padding: '6px 8px', borderRadius: 6, cursor: 'pointer',
                  background: selected.has(item) ? C.primaryLt : 'transparent',
                  fontSize: 13, color: C.text,
                }}
                onMouseEnter={(e) => { if (!selected.has(item)) e.currentTarget.style.background = C.grayBg }}
                onMouseLeave={(e) => { if (!selected.has(item)) e.currentTarget.style.background = 'transparent' }}
              >
                <input
                  type="checkbox"
                  checked={selected.has(item)}
                  onChange={() => toggle(item)}
                />
                <span>{item}</span>
              </label>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function StatCard({ icon: Icon, label, value, color }) {
  return (
    <div style={{
      flex: 1, minWidth: 160, background: C.cardBg, border: `1px solid ${C.cardBorder}`,
      borderRadius: 10, padding: '12px 14px', display: 'flex', alignItems: 'center', gap: 10,
    }}>
      <div style={{
        width: 36, height: 36, borderRadius: 8, background: `${color}15`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <Icon size={18} color={color} />
      </div>
      <div>
        <div style={{ fontSize: 11, color: C.textMuted, textTransform: 'uppercase', letterSpacing: 0.4 }}>{label}</div>
        <div style={{ fontSize: 16, fontWeight: 700, color: C.text }}>{value}</div>
      </div>
    </div>
  )
}

export default function OneSizePage() {
  const [running, setRunning]           = useState(false)
  const [exporting, setExporting] = useState(false)
  const [result, setResult]       = useState(null)
  const [error, setError]         = useState('')
  const [rdcOptions, setRdcOptions]     = useState([])
  const [selectedRdcs, setSelectedRdcs] = useState([])
  const [ssnOptions, setSsnOptions]     = useState([])
  const [selectedSsns, setSelectedSsns] = useState(['A', 'OC', 'S'])

  useEffect(() => {
    let cancelled = false
    oneSizeAPI.listRdcs()
      .then((res) => {
        if (cancelled) return
        setRdcOptions(res?.data?.data?.rdcs || [])
      })
      .catch(() => { /* silent — empty list disables the picker */ })
    oneSizeAPI.listSsns()
      .then((res) => {
        if (cancelled) return
        const list = res?.data?.data?.ssns || []
        const defaults = res?.data?.data?.defaults || ['A', 'OC', 'S']
        setSsnOptions(list)
        // Re-align the default selection to whatever is actually present —
        // if e.g. 'OC' doesn't exist for this sequence, drop it from the pick.
        setSelectedSsns(defaults.filter((d) => list.includes(d)))
      })
      .catch(() => { /* silent */ })
    return () => { cancelled = true }
  }, [])

  const columns      = result?.columns || []
  const previewRows  = result?.preview_rows || []
  const stages       = result?.stages || []
  const totalRows    = result?.total_rows || 0
  const stores       = result?.stores || 0
  const sequenceId   = result?.sequence_id
  const previewLimit = result?.preview_limit ?? 0

  const fmt = useMemo(() => new Intl.NumberFormat('en-IN'), [])

  const handleRun = async () => {
    setRunning(true)
    setError('')
    setResult(null)
    try {
      const { data } = await oneSizeAPI.run(1000, selectedRdcs, selectedSsns)
      setResult(data?.data || null)
      toast.success(data?.message || 'OneSize calculation complete')
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || 'Failed to run OneSize'
      setError(msg)
    } finally {
      setRunning(false)
    }
  }

  const handleExport = async () => {
    setExporting(true)
    try {
      const cacheKey = result?.cache_key || ''
      const res = await oneSizeAPI.exportCsvBlob(cacheKey, selectedRdcs, selectedSsns)
      const blob = new Blob([res.data], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `onesize_seq${sequenceId ?? 'latest'}.csv`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || 'CSV export failed'
      toast.error(typeof msg === 'string' ? msg : 'CSV export failed')
    } finally {
      setExporting(false)
    }
  }

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1400, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 14 }}>
      {/* Header */}
      <div>
        <div style={{ fontSize: 20, fontWeight: 800, color: C.text }}>OneSize</div>
        <div style={{ fontSize: 12, color: C.textMuted, marginTop: 2 }}>
          Latest MSA sequence → tag <code>applicable_sz</code> from MASTER_SZ_APPLICABLE → aggregate
          {' '}<code>count</code>, <code>maj_gen_cl_q</code>, <code>maj_cat_q</code> → cross-join with stores →
          {' '}<code>final_msa</code> gate → downstream allocation.
        </div>
      </div>

      {/* Filter logic panel */}
      <div style={{
        background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12,
        padding: 14,
      }}>
        <div style={{ fontSize: 11, color: C.textMuted, textTransform: 'uppercase', letterSpacing: 0.6, marginBottom: 8 }}>
          Filter logic — final_msa
        </div>
        <pre style={{
          margin: 0, padding: 10, background: C.inputBg, border: `1px solid ${C.inputBorder}`,
          borderRadius: 8, fontSize: 12, color: C.text, fontFamily: 'Consolas, monospace',
          overflow: 'auto', whiteSpace: 'pre-wrap', lineHeight: 1.55,
        }}>{`Step 1  — BEFORE cross-join: drop applicable_sz='N' rows
          (these fail the gate; can never pass)

Step 1a — BEFORE cross-join: compute row_or per MSA row
          row_or = (count        ≤ 2)   (≤2 sizes in this MAJ_CAT × GEN_ART × CLR)
                OR (maj_gen_cl_q ≤ 50)  (sum FNL_Q per MAJ_CAT × GEN_ART × CLR)
                OR (maj_cat_q    ≤ 400) (sum FNL_Q per MAJ_CAT)

Step 1b — BEFORE cross-join: drop row_or=FALSE rows
          (final filter — no ST_STATUS clause, so we can filter early)

Step 2  — Load stores from Master_ALC_INPUT_ST_MASTER (ST_STATUS for display only)
          Cross-join surviving MSA rows × stores on RDC.
          Every cross row is final_msa='Y' by construction — no post-join filter.

Step 3  — Continue to the downstream allocation pipeline.

Blank numeric cells are treated as 0 before ≤ comparisons (matches Excel).`}</pre>
      </div>

      {/* Controls */}
      <div style={{
        background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12,
        padding: 14, display: 'flex', alignItems: 'flex-end', gap: 14, flexWrap: 'wrap',
      }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <label style={{ fontSize: 11, color: C.textMuted, textTransform: 'uppercase', letterSpacing: 0.4 }}>
            RDC (optional)
          </label>
          <MultiSelect
            options={rdcOptions}
            value={selectedRdcs}
            onChange={setSelectedRdcs}
            disabled={running}
            allLabel="All RDCs"
            emptyHint="Leave empty for all RDCs"
          />
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <label style={{ fontSize: 11, color: C.textMuted, textTransform: 'uppercase', letterSpacing: 0.4 }}>
            SSN (season)
          </label>
          <MultiSelect
            options={ssnOptions}
            value={selectedSsns}
            onChange={setSelectedSsns}
            disabled={running}
            allLabel="All seasons"
            emptyHint="Leave empty to include every season"
          />
        </div>

        <button
          onClick={handleRun}
          disabled={running}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 8, padding: '9px 16px',
            background: running ? C.gray : C.primary, color: '#fff', border: 'none',
            borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: running ? 'wait' : 'pointer',
            boxShadow: '0 1px 2px rgba(0,0,0,0.08)',
          }}
        >
          {running ? <RefreshCw size={14} className="spin" /> : <Play size={14} />}
          {running ? 'Running…' : 'Execute'}
        </button>

        <button
          onClick={handleExport}
          disabled={!result || totalRows === 0 || exporting}
          title={!result ? 'Run first' : totalRows === 0 ? 'Nothing to export' : 'Download full CSV'}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 8, padding: '9px 16px',
            background: (!result || totalRows === 0 || exporting) ? C.grayBg : C.green,
            color: (!result || totalRows === 0 || exporting) ? C.textMuted : '#fff',
            border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600,
            cursor: (!result || totalRows === 0 || exporting) ? 'not-allowed' : 'pointer',
          }}
        >
          {exporting ? <RefreshCw size={14} className="spin" /> : <Download size={14} />}
          {exporting ? 'Exporting…' : 'Export CSV'}
        </button>

        <div style={{ flex: 1 }} />
        {sequenceId != null && (
          <div style={{ fontSize: 12, color: C.textMuted, display: 'flex', gap: 10, alignItems: 'center' }}>
            <span>sequence_id: <span style={{ color: C.text, fontWeight: 600 }}>{sequenceId}</span></span>
            {result?.cache_key && (
              <span style={{
                padding: '2px 8px', borderRadius: 12, background: '#dcfce7',
                color: '#166534', fontSize: 10, fontWeight: 600,
              }} title="Export will reuse the in-memory result (no recompute)">
                CACHED
              </span>
            )}
          </div>
        )}
      </div>

      {/* Progress */}
      {(running || stages.length > 0) && (
        <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
            <Filter size={14} color={C.primary} />
            <span style={{ fontSize: 13, fontWeight: 700, color: C.text }}>Pipeline progress</span>
          </div>
          <StageProgress stages={stages} running={running} />
        </div>
      )}

      {error && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
          background: C.redBg, border: `1px solid ${C.redBd}`, color: C.red,
          borderRadius: 10, fontSize: 13,
        }}>
          <AlertCircle size={16} /> {error}
        </div>
      )}

      {/* Stats */}
      {result && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <StatCard icon={Database} label="Result rows" value={fmt.format(totalRows)} color={C.primary} />
          <StatCard icon={Store} label="Stores" value={fmt.format(stores)} color={C.blue} />
          <StatCard icon={Layers} label="Preview rows shown" value={fmt.format(previewRows.length)} color={C.amber} />
        </div>
      )}

      {/* Preview */}
      {result && (
        <div style={{
          background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '10px 14px', borderBottom: `1px solid ${C.cardBorder}`, background: C.headerBg,
          }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>
              Preview (first {fmt.format(previewLimit)} of {fmt.format(totalRows)})
            </div>
            <div style={{ fontSize: 11, color: C.textMuted }}>
              {columns.length} columns · use CSV export to get all rows
            </div>
          </div>
          {previewRows.length === 0 ? (
            <div style={{ padding: 28, textAlign: 'center', color: C.textMuted, fontSize: 13 }}>
              No rows matched. Try a higher threshold.
            </div>
          ) : (
            <div style={{ maxHeight: 520, overflow: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead style={{ position: 'sticky', top: 0, background: C.headerBg, zIndex: 1 }}>
                  <tr>
                    {columns.map((c) => (
                      <th key={c} style={{
                        textAlign: 'left', padding: '8px 10px',
                        borderBottom: `1px solid ${C.cardBorder}`, fontWeight: 700,
                        color: C.textSub, whiteSpace: 'nowrap',
                      }}>{c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {previewRows.map((row, idx) => (
                    <tr key={idx} style={{ background: idx % 2 ? C.rowAlt : 'transparent' }}>
                      {columns.map((c) => {
                        const v = row[c]
                        const display = v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)
                        return (
                          <td key={c} style={{
                            padding: '6px 10px', borderBottom: `1px solid ${C.cardBorder}`,
                            color: C.text, whiteSpace: 'nowrap',
                          }}>{display}</td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
