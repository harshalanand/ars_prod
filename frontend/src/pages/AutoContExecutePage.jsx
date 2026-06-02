/**
 * AutoContExecutePage — Auto Cont % (SQL-direct pipeline)
 * Phase 2 onwards: creates a background JOB and polls its status until done.
 * Supports multi-preset selection and target (Store/Company/Both).
 */
import { useState, useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { autoContAPI, contribAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  Cpu, Play, CheckCircle, AlertTriangle, Loader2, Eye, X, RefreshCw,
} from 'lucide-react'
import { C } from '@/theme/colors'

const inp = {
  width: '100%', padding: '8px 12px', borderRadius: 8,
  border: `1px solid ${C.inputBorder}`, background: C.inputBg, color: C.text,
  fontSize: 13, boxSizing: 'border-box',
}

export default function AutoContExecutePage() {
  const nav = useNavigate()
  const [groupingCols, setGroupingCols] = useState(['MACRO_MVGR'])
  const [presets, setPresets] = useState([])
  const [majcats, setMajcats] = useState([])
  const [procInstalled, setProcInstalled] = useState(null)

  const [form, setForm] = useState({
    grouping_column: 'MACRO_MVGR',
    presets: [],          // empty = all
    majcats: [],          // empty = all
    target: 'Both',
    apply_mappings: true,
  })

  // active polling job
  const [jobId, setJobId] = useState(null)
  const [job, setJob] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const pollRef = useRef(null)

  // ── Load config on mount ─────────────────────────────────────────────────
  useEffect(() => {
    autoContAPI.status()
      .then(r => setProcInstalled(!!r.data?.data?.proc_installed))
      .catch(() => setProcInstalled(false))
    contribAPI.getGroupingColumns()
      .then(r => setGroupingCols(r.data?.data?.columns || ['MACRO_MVGR']))
      .catch(() => {})
    contribAPI.listPresets()
      .then(r => setPresets(r.data?.data?.presets || []))
      .catch(() => {})
  }, [])

  // ── Reload majcats when grouping_column changes ──────────────────────────
  useEffect(() => {
    contribAPI.getMajcats(form.grouping_column)
      .then(r => setMajcats(r.data?.data?.majcats || []))
      .catch(() => setMajcats([]))
  }, [form.grouping_column])

  // ── Job polling ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (!jobId) return
    const tick = async () => {
      try {
        const { data } = await autoContAPI.getJob(jobId)
        const j = data?.data?.job
        if (!j) return
        setJob(j)
        if (['completed','failed','cancelled'].includes(j.status)) {
          clearInterval(pollRef.current)
          pollRef.current = null
          if (j.status === 'completed') toast.success(`Job ${jobId} done in ${j.duration}s`)
          else if (j.status === 'failed') toast.error(`Job ${jobId} failed`)
          else toast(`Job ${jobId} cancelled`)
        }
      } catch (e) {
        // soft fail — keep polling
      }
    }
    tick()
    pollRef.current = setInterval(tick, 2000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [jobId])

  const togglePreset = (p) => setForm(f => ({
    ...f,
    presets: f.presets.includes(p) ? f.presets.filter(x => x !== p) : [...f.presets, p],
  }))
  const toggleMajcat = (m) => setForm(f => ({
    ...f,
    majcats: f.majcats.includes(m) ? f.majcats.filter(x => x !== m) : [...f.majcats, m],
  }))

  const runPipeline = async () => {
    if (procInstalled === false) {
      toast.error('Stored procedure not installed')
      return
    }
    setSubmitting(true)
    setJob(null)
    try {
      const { data } = await autoContAPI.execute(form)
      const id = data?.data?.job_id
      if (!id) throw new Error(data?.message || 'no job_id returned')
      setJobId(id)
      toast.success(`Job ${id} started`)
    } catch (e) {
      const msg = e.response?.data?.detail || e.response?.data?.message || e.message
      toast.error(String(msg).slice(0, 200))
    } finally {
      setSubmitting(false)
    }
  }

  const cancelJob = async () => {
    if (!jobId) return
    try { await autoContAPI.cancelJob(jobId); toast('Cancellation requested') }
    catch { toast.error('Cancel failed') }
  }

  const statusColor = (s) => ({
    pending:   '#64748b',
    running:   C.primary,
    completed: C.green,
    failed:    C.red,
    cancelled: C.amber,
  })[s] || C.textMuted

  return (
    <div style={{ color: C.text }}>
      <h1 style={{ fontSize: 20, fontWeight: 800, margin: '0 0 16px', display: 'flex', alignItems: 'center', gap: 10 }}>
        <Cpu size={20} color={C.primary} /> Auto Cont % — Execute (SQL-direct)
      </h1>

      {procInstalled === false && (
        <div style={{
          padding: '10px 14px', marginBottom: 14, borderRadius: 8,
          background: '#fef2f2', border: '1px solid #fecaca', color: '#991b1b',
          fontSize: 12, display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <AlertTriangle size={16} />
          <span><b>sp_AutoContCompute is not installed.</b> Run <code>backend/sql/sp_AutoContCompute.sql</code> against Rep_Data first.</span>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {/* ───────── Left: Form ───────── */}
        <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 18 }}>
          <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 14 }}>Pipeline parameters</div>

          <label style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: 'uppercase' }}>Grouping Column</label>
          <select value={form.grouping_column} onChange={e => setForm(f => ({ ...f, grouping_column: e.target.value }))}
            style={{ ...inp, marginBottom: 12 }}>
            {groupingCols.map(c => <option key={c} value={c}>{c}</option>)}
          </select>

          <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
            <div style={{ flex: 1 }}>
              <label style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: 'uppercase' }}>Target</label>
              <select value={form.target} onChange={e => setForm(f => ({ ...f, target: e.target.value }))} style={inp}>
                <option value="Both">Both</option>
                <option value="Store">Store only</option>
                <option value="Company">Company only</option>
              </select>
            </div>
            <div style={{ flex: 1, display: 'flex', alignItems: 'end', paddingBottom: 8 }}>
              <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                <input type="checkbox" checked={form.apply_mappings}
                  onChange={e => setForm(f => ({ ...f, apply_mappings: e.target.checked }))} />
                Apply mappings
              </label>
            </div>
          </div>

          <label style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: 'uppercase' }}>
            Presets ({form.presets.length === 0 ? `all (${presets.length})` : `${form.presets.length} selected`})
          </label>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, maxHeight: 140, overflowY: 'auto', marginTop: 6, marginBottom: 12 }}>
            {presets.map(p => {
              const sel = form.presets.includes(p.preset_name)
              return (
                <button key={p.preset_name} onClick={() => togglePreset(p.preset_name)} style={{
                  padding: '4px 12px', borderRadius: 16, fontSize: 11, fontWeight: 600, cursor: 'pointer',
                  border: `1.5px solid ${sel ? C.primary : '#e2e8f0'}`,
                  background: sel ? C.primary : '#fff', color: sel ? '#fff' : C.textSub,
                }}>
                  {sel ? '✓ ' : ''}{p.preset_name}
                </button>
              )
            })}
            {presets.length === 0 && (
              <div style={{ fontSize: 11, color: C.textMuted }}>No presets defined yet. Go to <b>Presets</b>.</div>
            )}
          </div>

          <label style={{ fontSize: 11, fontWeight: 700, color: C.textSub, textTransform: 'uppercase' }}>
            MAJ_CATs ({form.majcats.length === 0 ? `all (${majcats.length})` : `${form.majcats.length} selected`})
          </label>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, maxHeight: 160, overflowY: 'auto', marginTop: 6, marginBottom: 16 }}>
            {majcats.map(m => {
              const sel = form.majcats.includes(m)
              return (
                <button key={m} onClick={() => toggleMajcat(m)} style={{
                  padding: '4px 10px', borderRadius: 16, fontSize: 11, fontWeight: 600, cursor: 'pointer',
                  border: `1.5px solid ${sel ? C.primary : '#e2e8f0'}`,
                  background: sel ? C.primary : '#fff', color: sel ? '#fff' : C.textSub,
                }}>{m}</button>
              )
            })}
          </div>

          <button onClick={runPipeline}
            disabled={submitting || procInstalled === false || (job && ['pending','running'].includes(job.status))}
            style={{
              width: '100%', padding: '11px', borderRadius: 8, fontSize: 14, fontWeight: 700,
              border: 'none',
              cursor: (submitting || procInstalled === false) ? 'not-allowed' : 'pointer',
              background: (submitting || procInstalled === false) ? '#cbd5e1' : C.primary,
              color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8,
            }}>
            {submitting ? <><Loader2 size={16} className="spin" /> Submitting…</> : <><Play size={16} /> Start job</>}
          </button>
          <div style={{ fontSize: 10, color: C.textMuted, marginTop: 8, lineHeight: 1.5 }}>
            Each preset runs <code>sp_AutoContCompute</code>; results are combined horizontally
            (<code>col|preset</code>) and (optionally) mapping assignments are applied via SQL UPDATE.
            Intermediate per-preset tables are dropped after combine.
          </div>
        </div>

        {/* ───────── Right: Job status ───────── */}
        <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 18, minHeight: 260 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <div style={{ fontSize: 14, fontWeight: 700 }}>Job status</div>
            <button onClick={() => nav('/auto-cont/jobs')} style={{
              fontSize: 11, padding: '4px 10px', borderRadius: 6, fontWeight: 600,
              border: `1px solid ${C.primaryBd}`, background: C.primaryLight, color: C.primary,
              cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
            }}><Eye size={12} /> All jobs</button>
          </div>

          {!jobId && !job && (
            <div style={{ fontSize: 12, color: C.textMuted }}>
              No active job. Configure parameters on the left and click <b>Start job</b>.
            </div>
          )}

          {job && (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
                <span style={{
                  display: 'inline-block', width: 9, height: 9, borderRadius: '50%',
                  background: statusColor(job.status),
                  boxShadow: job.status === 'running' ? `0 0 10px ${statusColor(job.status)}` : 'none',
                  animation: job.status === 'running' ? 'pulse 1.4s infinite' : 'none',
                }} />
                <div style={{ fontSize: 13, fontWeight: 700, color: statusColor(job.status) }}>
                  {job.status.toUpperCase()}
                </div>
                <div style={{ fontSize: 11, color: C.textMuted, marginLeft: 8 }}>job <code>{jobId}</code></div>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '110px 1fr', gap: 8, fontSize: 12, marginBottom: 12 }}>
                <div style={{ color: C.textMuted }}>Progress</div>
                <div>{job.progress || '—'}</div>
                <div style={{ color: C.textMuted }}>Duration</div>
                <div>{job.duration ? `${job.duration}s` : (job.status === 'running' ? '…' : '—')}</div>
                <div style={{ color: C.textMuted }}>Detail rows</div>
                <div>{(job.detail_rows || 0).toLocaleString()} {job.detail_table && <span style={{ color: C.textMuted }}>· {job.detail_table}</span>}</div>
                <div style={{ color: C.textMuted }}>Company rows</div>
                <div>{(job.company_rows || 0).toLocaleString()} {job.company_table && <span style={{ color: C.textMuted }}>· {job.company_table}</span>}</div>
              </div>

              {/* Log tail */}
              {job.log?.length > 0 && (
                <div style={{
                  background: '#f8fafc', border: `1px solid ${C.cardBorder}`,
                  borderRadius: 8, padding: 8, maxHeight: 160, overflowY: 'auto',
                  fontFamily: 'monospace', fontSize: 10, lineHeight: 1.5,
                }}>
                  {job.log.slice(-12).map((entry, i) => (
                    <div key={i} style={{ color: entry.error ? C.red : C.textSub }}>
                      {entry.step || entry.action}: {JSON.stringify({ ...entry, step: undefined, action: undefined }).slice(0, 180)}
                    </div>
                  ))}
                </div>
              )}

              {job.error && (
                <div style={{
                  marginTop: 10, padding: 10, borderRadius: 8,
                  background: '#fef2f2', border: '1px solid #fecaca', color: '#991b1b',
                  fontSize: 11, fontFamily: 'monospace', whiteSpace: 'pre-wrap',
                }}>{job.error}</div>
              )}

              <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                {['pending','running'].includes(job.status) && (
                  <button onClick={cancelJob} style={{
                    padding: '6px 14px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                    border: `1px solid ${C.red}`, background: '#fff', color: C.red,
                    cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
                  }}><X size={12} /> Cancel</button>
                )}
                {job.status === 'completed' && (job.detail_table || job.company_table) && (
                  <button onClick={() => nav('/auto-cont/review')} style={{
                    padding: '6px 14px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                    border: 'none', background: C.green, color: '#fff',
                    cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
                  }}><CheckCircle size={12} /> View results</button>
                )}
                <button onClick={() => { setJobId(null); setJob(null) }} style={{
                  padding: '6px 14px', borderRadius: 6, fontSize: 12, fontWeight: 700,
                  border: `1px solid ${C.cardBorder}`, background: '#fff', color: C.textSub,
                  cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
                }}><RefreshCw size={12} /> Start new</button>
              </div>
            </div>
          )}
        </div>
      </div>

      <style>{`
        .spin { animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
      `}</style>
    </div>
  )
}
