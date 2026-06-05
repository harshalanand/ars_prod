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
import { Play, Download, RefreshCw, CheckCircle2, AlertCircle, Database, Layers, Store, X, Square, Trash2 } from 'lucide-react'
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

function ProgressBar({ stages, running }) {
  const orderedKeys = Object.keys(STAGE_LABELS)
  const total = orderedKeys.length
  const completedKeys = new Set((stages || []).map((s) => s.stage))
  const completedCount = orderedKeys.filter((k) => completedKeys.has(k)).length

  // Pick the label to display: when running, show the next pending stage;
  // when finished, show "Complete"; when idle with no stages, hide.
  const allDone = completedCount >= total
  const nextKey = orderedKeys.find((k) => !completedKeys.has(k))
  const activeKey = running
    ? (nextKey || orderedKeys[orderedKeys.length - 1])
    : (allDone ? null : (stages?.length ? orderedKeys[completedCount - 1] : null))

  if (!running && !stages?.length) return null

  const pct = Math.round((completedCount / total) * 100)
  const stepNum = Math.min(completedCount + (running ? 1 : 0), total)
  const label = allDone && !running
    ? 'Complete'
    : (activeKey ? STAGE_LABELS[activeKey] : 'Starting…')

  const stopped = !running && !allDone

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 13 }}>
        {allDone && !running ? (
          <CheckCircle2 size={16} color={C.green} />
        ) : stopped ? (
          <AlertCircle size={16} color={C.amber} />
        ) : (
          <RefreshCw size={16} color={C.primary} className="spin" />
        )}
        <span style={{ color: C.textMuted, fontWeight: 600, minWidth: 92 }}>
          Step {stepNum} of {total}
        </span>
        <span
          key={activeKey || 'done'}
          style={{
            color: C.text,
            flex: 1,
            animation: 'stepFade 280ms ease-out',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {label}
        </span>
        <span style={{ color: C.textMuted, fontVariantNumeric: 'tabular-nums' }}>{pct}%</span>
      </div>
      <div style={{
        position: 'relative', height: 8, borderRadius: 6,
        background: C.grayBg, overflow: 'hidden',
      }}>
        <div style={{
          position: 'absolute', inset: 0,
          width: `${pct}%`,
          background: allDone && !running ? C.green : stopped ? C.amber : C.primary,
          borderRadius: 6,
          transition: 'width 320ms ease-out, background 200ms linear',
        }} />
      </div>
      <style>{`
        .spin { animation: spin 0.9s linear infinite }
        @keyframes spin { to { transform: rotate(360deg) } }
        @keyframes stepFade {
          from { opacity: 0; transform: translateY(4px) }
          to   { opacity: 1; transform: translateY(0) }
        }
      `}</style>
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
        disabled={disabled}
        style={{
          minWidth: 200, padding: '8px 10px', border: `1px solid ${C.inputBorder}`,
          borderRadius: 8, fontSize: 13, background: C.inputBg, color: C.text,
          textAlign: 'left', cursor: disabled ? 'not-allowed' : 'pointer',
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
      {open && (
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
              {options.length === 0
                ? 'No options loaded — backend lookup failed. Use Retry above.'
                : `${emptyHint} (${options.length} available)`}
            </div>
            {options.length === 0 && (
              <div style={{ padding: '12px 8px', fontSize: 12, color: C.textMuted, textAlign: 'center' }}>
                Nothing to pick yet.
              </div>
            )}
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

const POLL_INTERVAL_MS = 800
const JOB_STORAGE_KEY = 'ars.onesize.jobId'

export default function OneSizePage() {
  // Lazy initializer — restore jobId from localStorage so jobs survive navigation.
  // The backend keeps the job alive in its in-memory registry until it finishes
  // or the user deletes it; we just need to remember which id to poll.
  const [job, setJob]                   = useState(null)
  const [jobId, setJobId]               = useState(() => {
    try { return localStorage.getItem(JOB_STORAGE_KEY) || null }
    catch { return null }
  })
  const [starting, setStarting]         = useState(false)  // brief: POST /jobs in flight
  const [cancelling, setCancelling]     = useState(false)
  const [deleting, setDeleting]         = useState(false)
  const [exporting, setExporting]       = useState(false)
  const [error, setError]               = useState('')
  const [rdcOptions, setRdcOptions]     = useState([])
  const [selectedRdcs, setSelectedRdcs] = useState([])
  const [ssnOptions, setSsnOptions]     = useState([])
  const [selectedSsns, setSelectedSsns] = useState(['A', 'OC', 'S'])
  const [optionsError, setOptionsError] = useState('')
  const [optionsLoading, setOptionsLoading] = useState(false)
  const [optionsRetryToken, setOptionsRetryToken] = useState(0)

  // Load RDC + SSN dropdown options. Retry by bumping optionsRetryToken.
  // Silent catches used to leave the dropdowns disabled with no feedback —
  // now any failure surfaces as a chip with a Retry button.
  useEffect(() => {
    let stopped = false
    const run = async () => {
      setOptionsLoading(true)
      setOptionsError('')
      let firstErr = ''
      try {
        const res = await oneSizeAPI.listRdcs()
        if (stopped) return
        setRdcOptions(res?.data?.data?.rdcs || [])
      } catch (e) {
        firstErr = e?.response?.data?.detail || e.message || 'Failed to load RDCs'
      }
      try {
        const res = await oneSizeAPI.listSsns()
        if (stopped) return
        const list = res?.data?.data?.ssns || []
        const defaults = res?.data?.data?.defaults || ['A', 'OC', 'S']
        setSsnOptions(list)
        // Re-align the default selection to whatever is actually present —
        // if e.g. 'OC' doesn't exist for this sequence, drop it from the pick.
        setSelectedSsns(defaults.filter((d) => list.includes(d)))
      } catch (e) {
        if (!firstErr) firstErr = e?.response?.data?.detail || e.message || 'Failed to load SSNs'
      }
      if (stopped) return
      if (firstErr) setOptionsError(firstErr)
      setOptionsLoading(false)
    }
    run()
    return () => { stopped = true }
  }, [optionsRetryToken])

  const handleRetryOptions = () => setOptionsRetryToken((t) => t + 1)

  // Persist jobId across navigation. Cleared on null (delete / 404 / fresh slate).
  useEffect(() => {
    try {
      if (jobId) localStorage.setItem(JOB_STORAGE_KEY, jobId)
      else       localStorage.removeItem(JOB_STORAGE_KEY)
    } catch { /* private mode etc. — nothing we can do */ }
  }, [jobId])

  // Poll the job until it reaches a terminal state.
  useEffect(() => {
    if (!jobId) return
    if (job && job.status && job.status !== 'running') return
    let stopped = false
    let timer

    const tick = async () => {
      try {
        const { data } = await oneSizeAPI.getJob(jobId, true)
        if (stopped) return
        const snap = data?.data || null
        setJob(snap)
        if (snap && snap.status === 'running') {
          timer = setTimeout(tick, POLL_INTERVAL_MS)
        } else if (snap?.status === 'failed') {
          toast.error(snap.error || 'OneSize job failed')
        } else if (snap?.status === 'cancelled') {
          toast('OneSize job cancelled', { icon: '⏹' })
        } else if (snap?.status === 'completed') {
          if (snap.persist_error) {
            toast.error(`Compute OK (${snap.total_rows} rows) but DB save failed`)
          } else if (snap.persisted_rows != null) {
            toast.success(`OneSize: ${snap.total_rows} rows · saved ${snap.persisted_rows} to DB`)
          } else {
            toast.success(`OneSize: ${snap.total_rows} rows`)
          }
        }
      } catch (e) {
        if (stopped) return
        // Backend forgot this job (process restart, LRU eviction, manual delete
        // from another tab). Treat as "gone" — clear state silently so the UI
        // returns to the idle Execute state instead of showing an error.
        if (e?.response?.status === 404) {
          setJob(null)
          setJobId(null)
          return
        }
        const msg = e?.response?.data?.detail || e.message || 'Polling failed'
        setError(msg)
      }
    }
    tick()
    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
  }, [jobId, job?.status])

  // Derived view-model — keep older variable names so the rest of the JSX is unchanged.
  const result       = job && job.status === 'completed' ? job : null
  const stages       = job?.stages || []
  const columns      = result?.columns || []
  const previewRows  = result?.preview_rows || []
  const totalRows    = result?.total_rows || 0
  const stores       = result?.stores || 0
  const sequenceId   = result?.sequence_id ?? job?.sequence_id
  const previewLimit = result?.preview_limit ?? 0
  const running      = !!job && job.status === 'running'
  const terminal     = !!job && job.status && job.status !== 'running'

  const fmt = useMemo(() => new Intl.NumberFormat('en-IN'), [])

  const handleRun = async () => {
    setStarting(true)
    setError('')
    setJob(null)
    setJobId(null)
    try {
      const { data } = await oneSizeAPI.startJob(1000, selectedRdcs, selectedSsns)
      const snap = data?.data
      if (snap?.job_id) {
        setJob(snap)
        setJobId(snap.job_id)
      } else {
        setError('Backend did not return a job_id')
      }
    } catch (e) {
      const msg = e?.response?.data?.detail || e.message || 'Failed to start OneSize job'
      setError(msg)
    } finally {
      setStarting(false)
    }
  }

  const handleCancel = async () => {
    if (!jobId || !running) return
    setCancelling(true)
    try {
      await oneSizeAPI.cancelJob(jobId)
      toast('Cancelling — will stop at the next stage boundary', { icon: '⏳' })
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message || 'Cancel failed')
    } finally {
      setCancelling(false)
    }
  }

  const handleDelete = async () => {
    if (!jobId) return
    setDeleting(true)
    try {
      await oneSizeAPI.deleteJob(jobId)
      setJob(null)
      setJobId(null)
      setError('')
    } catch (e) {
      toast.error(e?.response?.data?.detail || e.message || 'Delete failed')
    } finally {
      setDeleting(false)
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

      {/* Options-load error chip */}
      {(optionsError || (!optionsLoading && rdcOptions.length === 0 && ssnOptions.length === 0)) && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px',
          background: '#fef3c7', border: '1px solid #fcd34d', color: '#92400e',
          borderRadius: 10, fontSize: 12,
        }}>
          <AlertCircle size={14} />
          <span style={{ flex: 1 }}>
            {optionsError
              ? `Couldn't load filter options: ${optionsError}`
              : 'Filter options haven\'t loaded yet — backend lookup may be unavailable.'}
          </span>
          <button
            onClick={handleRetryOptions}
            disabled={optionsLoading}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6, padding: '4px 10px',
              background: '#fff', border: '1px solid #fcd34d', color: '#92400e',
              borderRadius: 6, fontSize: 12, fontWeight: 600,
              cursor: optionsLoading ? 'wait' : 'pointer',
            }}
          >
            <RefreshCw size={12} className={optionsLoading ? 'spin' : ''} />
            {optionsLoading ? 'Retrying…' : 'Retry'}
          </button>
        </div>
      )}

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
          disabled={running || starting}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 8, padding: '9px 16px',
            background: (running || starting) ? C.gray : C.primary, color: '#fff', border: 'none',
            borderRadius: 8, fontSize: 13, fontWeight: 600,
            cursor: (running || starting) ? 'wait' : 'pointer',
            boxShadow: '0 1px 2px rgba(0,0,0,0.08)',
          }}
        >
          {(running || starting) ? <RefreshCw size={14} className="spin" /> : <Play size={14} />}
          {starting ? 'Starting…' : running ? 'Running…' : 'Execute'}
        </button>

        {running && (
          <button
            onClick={handleCancel}
            disabled={cancelling}
            title="Stop the running job at the next stage boundary"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 8, padding: '9px 16px',
              background: cancelling ? C.grayBg : C.amber, color: '#fff', border: 'none',
              borderRadius: 8, fontSize: 13, fontWeight: 600,
              cursor: cancelling ? 'wait' : 'pointer',
            }}
          >
            {cancelling ? <RefreshCw size={14} className="spin" /> : <Square size={14} />}
            {cancelling ? 'Cancelling…' : 'Cancel'}
          </button>
        )}

        {terminal && jobId && (
          <button
            onClick={handleDelete}
            disabled={deleting}
            title="Remove this job and its cached result"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 8, padding: '9px 16px',
              background: deleting ? C.grayBg : C.red, color: '#fff', border: 'none',
              borderRadius: 8, fontSize: 13, fontWeight: 600,
              cursor: deleting ? 'wait' : 'pointer',
            }}
          >
            {deleting ? <RefreshCw size={14} className="spin" /> : <Trash2 size={14} />}
            {deleting ? 'Deleting…' : 'Delete job'}
          </button>
        )}

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
        {(sequenceId != null || jobId) && (
          <div style={{ fontSize: 12, color: C.textMuted, display: 'flex', gap: 10, alignItems: 'center' }}>
            {jobId && (
              <span title={jobId}>
                job: <span style={{ color: C.text, fontWeight: 600 }}>{jobId.slice(0, 8)}</span>
              </span>
            )}
            {sequenceId != null && (
              <span>sequence_id: <span style={{ color: C.text, fontWeight: 600 }}>{sequenceId}</span></span>
            )}
            {job?.status && job.status !== 'completed' && (
              <span style={{
                padding: '2px 8px', borderRadius: 12,
                background: job.status === 'failed' ? '#fee2e2'
                          : job.status === 'cancelled' ? '#fef3c7' : '#dbeafe',
                color: job.status === 'failed' ? '#991b1b'
                     : job.status === 'cancelled' ? '#92400e' : '#1e40af',
                fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: 0.4,
              }}>
                {job.status}
              </span>
            )}
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
          <ProgressBar stages={stages} running={running} />
        </div>
      )}

      {(error || job?.error) && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
          background: C.redBg, border: `1px solid ${C.redBd}`, color: C.red,
          borderRadius: 10, fontSize: 13,
        }}>
          <AlertCircle size={16} /> {error || job?.error}
        </div>
      )}

      {job?.persist_error && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '10px 14px',
          background: '#fef3c7', border: '1px solid #fcd34d', color: '#92400e',
          borderRadius: 10, fontSize: 13,
        }}>
          <AlertCircle size={16} /> {job.persist_error} — result is still available for CSV export.
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
