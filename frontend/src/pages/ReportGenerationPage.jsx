/**
 * ReportGenerationPage — the Report Generation hub.
 * A report = ordered steps (SQL procedures + code steps) with a trigger
 * (schedule | event | manual) and an output (folder | snowflake | both).
 * List existing reports, generate on-demand, and create/edit definitions.
 */
import { useState, useEffect, useRef, Fragment } from 'react'
import { reportGenAPI } from '@/services/api'
import toast from 'react-hot-toast'
import {
  FileText, RefreshCw, Plus, Play, Trash2, Pencil, Clock, Link2,
  MousePointerClick, X, CheckCircle, AlertTriangle, Loader2, ChevronDown,
  Mail, Scissors, CalendarClock, Zap, Hand, LayoutList, FolderOutput, Database,
  Square,
} from 'lucide-react'
import { C } from '@/theme/colors'

const EMPTY_FORM = {
  report_id: null, name: '', description: '', steps: [],
  output_type: 'folder', base_dir: '', file_format: 'csv', folder_per_run: true,
  trigger_type: 'manual', schedule_config: { freq: 'daily', times: ['07:00'], weekdays: [0, 1, 2, 3, 4], days: [1], every_n_hours: 2, start: '12:00', end: '23:00', datetime: '' },
  trigger_event: 'listing.approved', enabled: true,
  split_config: {
    enabled: false, method: 'product', max_rows: 1000000,
    product_hierarchy: ['SEG', 'DIV', 'SUB_DIV', 'MAJ_CAT'],
    store_hierarchy: ['ZONE', 'REG', 'STORE'],
  },
  email_config: {
    enabled: false, to: [], cc: [], bcc: [], subject: '', body: '',
    attach_format: 'xlsx', attach_scope: 'all', zip: false, notify_on_fail: false,
  },
  snowflake_config: { database: '', schema: '', warehouse: '', targets: [] },
}

const statusColor = (s) => ({
  completed: C.green, failed: C.red, running: C.primary,
  skipped: C.amber, cancelled: C.amber, pending: C.textMuted,
}[s] || C.textMuted)

const statusIcon = (s, size = 12) => {
  const st = { color: statusColor(s) }
  if (s === 'running') return <Loader2 size={size} style={st} className="spin" />
  if (s === 'completed') return <CheckCircle size={size} style={st} />
  if (s === 'failed') return <AlertTriangle size={size} style={st} />
  if (s === 'cancelled') return <Square size={size} style={st} />
  return <Clock size={size} style={st} />
}

export default function ReportGenerationPage() {
  const [reports, setReports] = useState([])
  const [loading, setLoading] = useState(false)
  const [codeSteps, setCodeSteps] = useState([])
  const [procs, setProcs] = useState([])
  const [events, setEvents] = useState([])
  const [sched, setSched] = useState(null)        // scheduler status (running/queued)
  const [form, setForm] = useState(null)          // null = form hidden
  const [expanded, setExpanded] = useState(null)  // report_id whose runs show
  const [filter, setFilter] = useState('all')      // all|schedule|event|manual|enabled
  const [runs, setRuns] = useState({})            // report_id -> runs[]
  const [newProc, setNewProc] = useState('')
  const pollRef = useRef(null)

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await reportGenAPI.list()
      setReports(data?.data || [])
    } catch { toast.error('Failed to load reports') }
    finally { setLoading(false) }
    reportGenAPI.status().then(({ data }) => setSched(data?.data || null)).catch(() => {})
  }

  useEffect(() => {
    load()
    reportGenAPI.codeSteps().then(({ data }) => setCodeSteps(data?.data || [])).catch(() => {})
    reportGenAPI.procedures().then(({ data }) => setProcs(data?.data || [])).catch(() => {})
    reportGenAPI.events().then(({ data }) => setEvents(data?.data || [])).catch(() => {})
    pollRef.current = setInterval(load, 5000)
    return () => clearInterval(pollRef.current)
  }, [])

  const openRuns = async (id) => {
    if (expanded === id) { setExpanded(null); return }
    setExpanded(id)
    try {
      const { data } = await reportGenAPI.runs(id)
      setRuns(r => ({ ...r, [id]: data?.data || [] }))
    } catch { toast.error('Failed to load runs') }
  }

  const generate = async (id) => {
    try { await reportGenAPI.runNow(id); toast.success('Report queued'); setTimeout(load, 800) }
    catch { toast.error('Could not queue report') }
  }

  const stop = async (id) => {
    try {
      const { data } = await reportGenAPI.cancel(id)
      toast(data?.success ? 'Stopping report…' : (data?.message || 'Not running'))
      setTimeout(load, 800)
    } catch { toast.error('Could not stop report') }
  }

  const toggle = async (r) => {
    try { await reportGenAPI.toggle(r.REPORT_ID, !r.ENABLED); load() }
    catch { toast.error('Toggle failed') }
  }

  const remove = async (id) => {
    if (!confirm('Delete this report?')) return
    try { await reportGenAPI.remove(id); toast.success('Deleted'); load() }
    catch { toast.error('Delete failed') }
  }

  const startEdit = (r) => {
    setForm({
      report_id: r.REPORT_ID, name: r.NAME || '', description: r.DESCRIPTION || '',
      steps: Array.isArray(r.STEPS) ? r.STEPS : [],
      output_type: r.OUTPUT_TYPE || 'folder', base_dir: r.BASE_DIR || '',
      file_format: r.FILE_FORMAT || 'csv',
      folder_per_run: r.FOLDER_PER_RUN === undefined ? true : !!r.FOLDER_PER_RUN,
      trigger_type: r.TRIGGER_TYPE || 'manual',
      schedule_config: r.SCHEDULE_CONFIG || EMPTY_FORM.schedule_config,
      trigger_event: r.TRIGGER_EVENT || 'listing.approved',
      enabled: !!r.ENABLED,
      split_config: r.SPLIT_CONFIG || { ...EMPTY_FORM.split_config },
      email_config: r.EMAIL_CONFIG || { ...EMPTY_FORM.email_config },
      snowflake_config: r.SNOWFLAKE_CONFIG || { ...EMPTY_FORM.snowflake_config },
    })
    window.scrollTo({ top: 9999, behavior: 'smooth' })
  }

  const addProc = (label) => {
    const name = newProc.trim()
    if (!name) return
    const step = { type: 'sql', name, params: {} }
    if (label && String(label).trim()) step.label = String(label).trim()
    setForm(f => ({ ...f, steps: [...f.steps, step] }))
    setNewProc('')
  }
  const addCode = (name) => {
    if (!name) return
    setForm(f => ({ ...f, steps: [...f.steps, { type: 'code', name, params: {} }] }))
  }
  const addQuery = (name, sql) => {
    if (!name.trim() || !sql.trim()) return toast.error('Give the query a name and SQL')
    setForm(f => ({ ...f, steps: [...f.steps, { type: 'query', name: name.trim(), params: { sql: sql.trim() } }] }))
  }
  const removeStep = (i) =>
    setForm(f => ({ ...f, steps: f.steps.filter((_, idx) => idx !== i) }))

  const save = async () => {
    if (!form.name.trim()) return toast.error('Name is required')
    if (!form.steps.length) return toast.error('Add at least one step')
    const body = {
      name: form.name, description: form.description || null, steps: form.steps,
      output_type: form.output_type, base_dir: form.base_dir || null,
      file_format: form.file_format, folder_per_run: form.folder_per_run,
      trigger_type: form.trigger_type,
      schedule_config: form.trigger_type === 'schedule' ? form.schedule_config : null,
      trigger_event: form.trigger_type === 'event' ? form.trigger_event : null,
      enabled: form.enabled,
      split_config: ((['folder', 'both'].includes(form.output_type) || form.email_config?.enabled) && form.split_config?.enabled)
        ? form.split_config : null,
      email_config: form.email_config?.enabled ? form.email_config : null,
      snowflake_config: ['snowflake', 'both'].includes(form.output_type) ? form.snowflake_config : null,
    }
    try {
      if (form.report_id) { await reportGenAPI.update(form.report_id, body); toast.success('Report updated') }
      else { await reportGenAPI.create(body); toast.success('Report created') }
      setForm(null); load()
    } catch (e) { toast.error(e?.response?.data?.detail || 'Save failed') }
  }

  const triggerBadge = (r) => {
    if (r.TRIGGER_TYPE === 'schedule') {
      const c = r.SCHEDULE_CONFIG || {}
      const times = (c.times && c.times.length) ? c.times.join('/') : (c.time || '')
      const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
      const wdays = (c.weekdays && c.weekdays.length ? c.weekdays : (c.weekday != null ? [c.weekday] : []))
        .map(i => DOW[i]).join(',')
      const mdays = (c.days && c.days.length ? c.days : (c.day != null ? [c.day] : [])).join(',')
      const label =
        c.freq === 'every_n_hours' ? `Every ${c.every_n_hours}h${c.start && c.end ? ` ${c.start}–${c.end}` : ''}`
        : c.freq === 'once' ? `Once ${(c.datetime || '').replace('T', ' ')}`
        : c.freq === 'weekly' ? `${wdays} ${times}`
        : c.freq === 'monthly' ? `Monthly d${mdays} ${times}`
        : `Daily ${times}`
      return <Badge icon={<Clock size={11} />} text={label} bg={C.primaryLight} fg={C.primary} />
    }
    if (r.TRIGGER_TYPE === 'event') {
      const ev = events.find(e => e.event === r.TRIGGER_EVENT)
      return <Badge icon={<Link2 size={11} />} text={`On ${ev?.label || r.TRIGGER_EVENT}`} bg="#f1f5f9" fg={C.textSub} />
    }
    return <Badge icon={<MousePointerClick size={11} />} text="Manual only" bg="#f1f5f9" fg={C.textSub} />
  }

  const stats = {
    total: reports.length,
    scheduled: reports.filter(r => r.TRIGGER_TYPE === 'schedule').length,
    event: reports.filter(r => r.TRIGGER_TYPE === 'event').length,
    manual: reports.filter(r => r.TRIGGER_TYPE === 'manual').length,
    enabled: reports.filter(r => r.ENABLED).length,
  }
  const matchesFilter = (r) => filter === 'all'
    || (filter === 'enabled' ? !!r.ENABLED : r.TRIGGER_TYPE === filter)
  const filteredReports = reports.filter(matchesFilter)
  const runningIds = new Set(sched?.running_report_ids || [])
  // Clicking a card toggles that filter on/off.
  const cardFilter = (key) => setFilter(f => (f === key ? 'all' : key))

  return (
    <div style={{ color: C.text }}>
      {/* Header bar */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 18 }}>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
          <div style={{ width: 40, height: 40, borderRadius: 10, background: C.primaryLight,
                        display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <FileText size={20} color={C.primary} />
          </div>
          <div>
            <h1 style={{ fontSize: 19, fontWeight: 800, margin: 0, display: 'flex', alignItems: 'center', gap: 10 }}>
              Report Generation
              {sched && (sched.running_count > 0 || sched.queued_count > 0) && (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, fontWeight: 700,
                               padding: '2px 9px', borderRadius: 20, background: C.primaryLight, color: C.primary }}>
                  <Loader2 size={11} className="spin" />
                  {sched.running_count} running · {sched.queued_count} queued
                </span>
              )}
            </h1>
            <p style={{ margin: '2px 0 0', fontSize: 12.5, color: C.textMuted }}>
              Build, schedule, and deliver data reports — stored procedures, SQL, and code.
            </p>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={load} style={btn()}>
            <RefreshCw size={12} className={loading ? 'spin' : ''} /> Refresh
          </button>
          <button onClick={() => setForm({ ...EMPTY_FORM })} style={btn(C.primary, '#fff')}>
            <Plus size={12} /> New report
          </button>
        </div>
      </div>

      {/* Summary stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 18 }}>
        <StatCard icon={<LayoutList size={16} />} label="Total reports" value={stats.total} tint={C.primary} bg={C.primaryLight} active={filter === 'all'} onClick={() => setFilter('all')} />
        <StatCard icon={<CalendarClock size={16} />} label="Scheduled" value={stats.scheduled} tint={C.blue} bg={C.blueBg} active={filter === 'schedule'} onClick={() => cardFilter('schedule')} />
        <StatCard icon={<Zap size={16} />} label="Event-triggered" value={stats.event} tint={C.amber} bg={C.amberBg} active={filter === 'event'} onClick={() => cardFilter('event')} />
        <StatCard icon={<Hand size={16} />} label="Manual" value={stats.manual} tint={C.gray} bg={C.grayBg} active={filter === 'manual'} onClick={() => cardFilter('manual')} />
        <StatCard icon={<CheckCircle size={16} />} label="Enabled" value={stats.enabled} tint={C.green} bg={C.greenBg} active={filter === 'enabled'} onClick={() => cardFilter('enabled')} />
      </div>

      {/* Reports table */}
      <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ background: C.headerBg, borderBottom: `1px solid ${C.cardBorder}` }}>
              <th style={th}>Report</th>
              <th style={th}>Trigger</th>
              <th style={th}>Output</th>
              <th style={th}>Last run</th>
              <th style={th}>Enabled</th>
              <th style={{ ...th, textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filteredReports.length === 0 && (
              <tr><td colSpan={6} style={{ padding: 40, textAlign: 'center', color: C.textMuted }}>
                {reports.length === 0
                  ? <>No reports yet. Click <b>New report</b> to create one.</>
                  : <>No reports match this filter. <button onClick={() => setFilter('all')} style={{ background: 'none', border: 'none', color: C.primary, cursor: 'pointer', textDecoration: 'underline' }}>Show all</button></>}
              </td></tr>
            )}
            {filteredReports.map(r => (
              <Fragment key={r.REPORT_ID}>
                <tr style={{ borderBottom: `1px solid ${C.cardBorder}` }}>
                  <td style={td}>
                    <button onClick={() => openRuns(r.REPORT_ID)}
                      style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                               display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700, color: C.text }}>
                      <ChevronDown size={13} style={{ transform: expanded === r.REPORT_ID ? 'rotate(180deg)' : 'none' }} />
                      {r.NAME}
                    </button>
                  </td>
                  <td style={td}>{triggerBadge(r)}</td>
                  <td style={td}>
                    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ color: C.textSub, textTransform: 'capitalize' }}>{r.OUTPUT_TYPE}</span>
                      {r.SPLIT_CONFIG?.enabled && <Scissors size={13} color={C.textMuted} title="Split into multiple files" />}
                      {r.EMAIL_CONFIG?.enabled && <Mail size={13} color={C.primary} title={`Emailed to ${(r.EMAIL_CONFIG.to || []).join(', ')}`} />}
                    </span>
                  </td>
                  <td style={td}>
                    {runningIds.has(r.REPORT_ID)
                      ? <span style={statusPill('running')}><Loader2 size={11} className="spin" /> running</span>
                      : r.LAST_STATUS
                        ? <span style={statusPill(r.LAST_STATUS)}>{statusIcon(r.LAST_STATUS, 11)} {r.LAST_STATUS}</span>
                        : <span style={{ color: C.textMuted }}>Never</span>}
                  </td>
                  <td style={td}>
                    <button onClick={() => toggle(r)} title={r.ENABLED ? 'Disable' : 'Enable'}
                      style={{ background: 'none', border: 'none', cursor: 'pointer',
                               color: r.ENABLED ? C.green : C.textMuted, fontWeight: 700 }}>
                      {r.ENABLED ? 'On' : 'Off'}
                    </button>
                  </td>
                  <td style={{ ...td, textAlign: 'right' }}>
                    <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                      {runningIds.has(r.REPORT_ID)
                        ? <button onClick={() => stop(r.REPORT_ID)} title="Stop this run" style={iconBtn(C.red)}><Square size={13} fill={C.red} /></button>
                        : <button onClick={() => generate(r.REPORT_ID)} title="Generate now" style={iconBtn(C.primary)}><Play size={13} /></button>}
                      <button onClick={() => startEdit(r)} title="Edit" style={iconBtn(C.textSub)}><Pencil size={13} /></button>
                      <button onClick={() => remove(r.REPORT_ID)} title="Delete" style={iconBtn(C.red)}><Trash2 size={13} /></button>
                    </div>
                  </td>
                </tr>
                {expanded === r.REPORT_ID && (
                  <tr style={{ background: '#f8fafc' }}>
                    <td colSpan={6} style={{ padding: '12px 16px' }}>
                      <RunHistory runs={runs[r.REPORT_ID]} onStop={() => stop(r.REPORT_ID)} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {/* Create / edit form */}
      {form && (
        <ReportForm
          form={form} setForm={setForm} codeSteps={codeSteps} events={events} procs={procs}
          newProc={newProc} setNewProc={setNewProc}
          addProc={addProc} addCode={addCode} addQuery={addQuery} removeStep={removeStep}
          onSave={save} onCancel={() => setForm(null)}
        />
      )}

      <style>{`.spin { animation: spin 1s linear infinite; } @keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  )
}

function RunHistory({ runs, onStop }) {
  if (!runs) return <div style={{ color: C.textMuted, fontSize: 11 }}>Loading runs…</div>
  if (!runs.length) return <div style={{ color: C.textMuted, fontSize: 11 }}>No runs yet.</div>
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
      <thead>
        <tr style={{ color: C.textMuted }}>
          <th style={{ ...th, fontSize: 10 }}>Status</th>
          <th style={{ ...th, fontSize: 10 }}>Trigger</th>
          <th style={{ ...th, fontSize: 10 }}>Session code</th>
          <th style={{ ...th, fontSize: 10, textAlign: 'right' }}>Rows</th>
          <th style={{ ...th, fontSize: 10 }}>Started</th>
          <th style={{ ...th, fontSize: 10 }}>Ended</th>
          <th style={{ ...th, fontSize: 10, textAlign: 'right' }}>Duration</th>
          <th style={{ ...th, fontSize: 10 }}>Folder</th>
        </tr>
      </thead>
      <tbody>
        {runs.map(run => (
          <tr key={run.RUN_ID} style={{ borderTop: `1px solid ${C.cardBorder}` }}>
            <td style={td}>
              <span style={{ display: 'flex', alignItems: 'center', gap: 5, color: statusColor(run.STATUS), textTransform: 'capitalize' }}>
                {statusIcon(run.STATUS, 11)} {run.STATUS}
                {run.STATUS === 'running' && onStop && (
                  <button onClick={onStop} title="Stop / clear this run"
                    style={{ background: 'none', border: 'none', cursor: 'pointer', color: C.red, padding: 0, marginLeft: 4, display: 'inline-flex' }}>
                    <Square size={11} fill={C.red} />
                  </button>
                )}
              </span>
            </td>
            <td style={{ ...td, color: C.textSub }}>{run.TRIGGER_SOURCE}</td>
            <td style={{ ...td, fontFamily: 'monospace' }}>{run.SESSION_CODE}</td>
            <td style={{ ...td, textAlign: 'right' }}>{(run.ROW_COUNT ?? 0).toLocaleString()}</td>
            <td style={{ ...td, color: C.textMuted }}>{fmtTime(run.STARTED_AT)}</td>
            <td style={{ ...td, color: C.textMuted }}>{fmtTime(run.COMPLETED_AT)}</td>
            <td style={{ ...td, textAlign: 'right', color: C.textSub }}>{fmtDuration(run.DURATION_MS)}</td>
            <td style={{ ...td, fontFamily: 'monospace', color: C.textMuted, maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={run.EXPORT_DIR}>{run.EXPORT_DIR || '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function ReportForm({ form, setForm, codeSteps, events, procs, newProc, setNewProc, addProc, addCode, addQuery, removeStep, onSave, onCancel }) {
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))
  const setSched = (k, v) => setForm(f => ({ ...f, schedule_config: { ...f.schedule_config, [k]: v } }))
  const setSplit = (k, v) => setForm(f => ({ ...f, split_config: { ...f.split_config, [k]: v } }))
  const setEmail = (k, v) => setForm(f => ({ ...f, email_config: { ...f.email_config, [k]: v } }))
  const setSnow = (k, v) => setForm(f => ({ ...f, snowflake_config: { ...f.snowflake_config, [k]: v } }))
  const parseAddrs = (s) => s.split(/[,;]/).map(x => x.trim()).filter(Boolean)
  const sc = form.schedule_config || {}
  const sp = form.split_config || {}
  const em = form.email_config || {}
  const sf = form.snowflake_config || {}
  const sfTargets = sf.targets || []
  const setTarget = (i, k, v) => setSnow('targets', sfTargets.map((t, idx) => idx === i ? { ...t, [k]: v } : t))
  const addTarget = () => setSnow('targets', [...sfTargets, { source_file: '', table: '', key_cols: [], watermark_col: '' }])
  const removeTarget = (i) => setSnow('targets', sfTargets.filter((_, idx) => idx !== i))
  const presetBtn = { background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 10, color: C.primary, textDecoration: 'underline', fontWeight: 600 }
  // Deliver-to derives from output_type (folder/snowflake) + email enabled.
  const dFolder = form.output_type === 'folder' || form.output_type === 'both'
  const dSnow = form.output_type === 'snowflake' || form.output_type === 'both'
  const dEmail = !!em.enabled
  const setDeliver = (folder, snow) =>
    set('output_type', folder && snow ? 'both' : snow ? 'snowflake' : folder ? 'folder' : 'email')
  const [showQuery, setShowQuery] = useState(false)
  const [qName, setQName] = useState('')
  const [qSql, setQSql] = useState('')
  const [procLabel, setProcLabel] = useState('')
  const [editIdx, setEditIdx] = useState(null)   // step index being edited (query)
  const editQuery = (i) => {
    const s = form.steps[i]
    setQName(s.name || '')
    setQSql(s.params?.sql || '')
    setEditIdx(i)
    setShowQuery(true)
  }
  const cancelQuery = () => { setShowQuery(false); setQName(''); setQSql(''); setEditIdx(null) }

  // Proc-step params editor (variables passed to the stored procedure).
  const [procEditIdx, setProcEditIdx] = useState(null)
  const [procParams, setProcParams] = useState([])   // [{name, value}]
  const [procOutName, setProcOutName] = useState('')
  const [procLoading, setProcLoading] = useState(false)
  const editProc = (i) => {
    const s = form.steps[i]
    const p = s.params || {}
    setProcEditIdx(i)
    setProcOutName(s.label || '')
    setProcParams(Object.keys(p).map(k => ({ name: k, value: String(p[k] ?? '') })))
  }
  const loadProcParams = async () => {
    const s = form.steps[procEditIdx]
    if (!s) return
    setProcLoading(true)
    try {
      const { data } = await reportGenAPI.procParams(s.name)
      const declared = (data?.data || [])
      // keep any values the user already typed, add any missing declared params
      setProcParams(prev => {
        const byName = Object.fromEntries(prev.map(r => [r.name, r.value]))
        return declared.map(d => ({ name: d.name, value: byName[d.name] ?? '', type: d.type }))
      })
      if (!declared.length) toast('This procedure has no input parameters')
    } catch { toast.error('Could not load procedure parameters') }
    finally { setProcLoading(false) }
  }
  const setProcParam = (i, k, v) => setProcParams(ps => ps.map((r, idx) => idx === i ? { ...r, [k]: v } : r))
  const addProcParam = () => setProcParams(ps => [...ps, { name: '', value: '' }])
  const removeProcParam = (i) => setProcParams(ps => ps.filter((_, idx) => idx !== i))
  const saveProc = () => {
    const params = {}
    for (const r of procParams) {
      const n = String(r.name || '').trim()
      if (!n) continue
      // A blank value is passed to the procedure as NULL (not an empty string),
      // so leaving a filter param empty means "no filter / all".
      const v = r.value
      params[n] = (v === '' || v == null) ? null : v
    }
    setForm(f => ({ ...f, steps: f.steps.map((st, idx) => {
      if (idx !== procEditIdx) return st
      const next = { ...st, params }
      if (procOutName.trim()) next.label = procOutName.trim(); else delete next.label
      return next
    }) }))
    setProcEditIdx(null); setProcParams([]); setProcOutName('')
  }
  const cancelProc = () => { setProcEditIdx(null); setProcParams([]); setProcOutName('') }
  const stepBadge = (t) => t === 'sql' ? { bg: C.primaryLight, fg: C.primary }
    : t === 'query' ? { bg: '#dcfce7', fg: '#166534' }
    : { bg: '#fef3c7', fg: '#92400e' }
  const submitQuery = () => {
    if (!qName.trim() || !qSql.trim()) { toast.error('Give the query a name and SQL'); return }
    if (editIdx != null) {
      // Update the existing query step in place.
      setForm(f => ({ ...f, steps: f.steps.map((st, idx) =>
        idx === editIdx ? { type: 'query', name: qName.trim(), params: { sql: qSql.trim() } } : st) }))
    } else {
      addQuery(qName, qSql)
    }
    setQName(''); setQSql(''); setShowQuery(false); setEditIdx(null)
  }
  return (
    <div style={{ background: C.cardBg, border: `1px solid ${C.cardBorder}`, borderRadius: 12, padding: 20, marginTop: 20 }}>
      <h2 style={{ fontSize: 15, fontWeight: 800, margin: '0 0 16px' }}>{form.report_id ? 'Edit report' : 'New report'}</h2>
      <div style={{ marginBottom: 14 }}>
        <Field label="Name"><input value={form.name} onChange={e => set('name', e.target.value)} style={inp} /></Field>
      </div>

      {/* Steps */}
      <label style={lbl}>Steps (run in order)</label>
      <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 8, overflow: 'hidden', marginBottom: 10 }}>
        {form.steps.length === 0 && <div style={{ padding: 12, color: C.textMuted, fontSize: 12 }}>No steps yet.</div>}
        {form.steps.map((s, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', borderBottom: i < form.steps.length - 1 ? `1px solid ${C.cardBorder}` : 'none' }}>
            <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 4,
                           background: stepBadge(s.type).bg, color: stepBadge(s.type).fg }}>{s.type.toUpperCase()}</span>
            <span style={{ fontFamily: 'monospace', fontSize: 12 }}>{s.name}</span>
            {s.label && (
              <span title="output file name" style={{ fontSize: 11, color: C.textSub, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                → <span style={{ fontFamily: 'monospace' }}>{s.label}</span>
              </span>
            )}
            {s.type === 'query' && (
              <span title={s.params?.sql} style={{ fontFamily: 'monospace', fontSize: 11, color: C.textMuted, maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {s.params?.sql}
              </span>
            )}
            {s.type === 'sql' && s.params && Object.keys(s.params).length > 0 && (
              <span title="parameters" style={{ fontFamily: 'monospace', fontSize: 11, color: C.textMuted }}>
                ({Object.entries(s.params).map(([k, v]) => `@${k}=${v == null || v === '' ? 'NULL' : v}`).join(', ')})
              </span>
            )}
            <div style={{ display: 'flex', gap: 4, marginLeft: 'auto' }}>
              {s.type === 'query' && (
                <button onClick={() => editQuery(i)} title="Edit query" style={iconBtn(C.textSub)}><Pencil size={13} /></button>
              )}
              {s.type === 'sql' && (
                <button onClick={() => editProc(i)} title="Edit parameters / output name" style={iconBtn(C.textSub)}><Pencil size={13} /></button>
              )}
              <button onClick={() => removeStep(i)} title="Remove step" style={iconBtn(C.textMuted)}><X size={13} /></button>
            </div>
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        <input value={newProc} onChange={e => setNewProc(e.target.value)} placeholder="pick or type a stored procedure"
               list="proc-list" onKeyDown={e => e.key === 'Enter' && (addProc(procLabel), setProcLabel(''))}
               style={{ ...inp, width: 240, fontFamily: 'monospace', fontSize: 12 }} />
        <datalist id="proc-list">
          {(procs || []).map(p => <option key={p.full} value={p.full}>{p.schema}</option>)}
        </datalist>
        <input value={procLabel} onChange={e => setProcLabel(e.target.value)} placeholder="output name (optional)"
               title="Custom file name for this procedure's output (defaults to the proc name)"
               style={{ ...inp, width: 180, fontSize: 12 }} />
        <button onClick={() => { addProc(procLabel); setProcLabel('') }} style={btn()}><Plus size={12} /> Add procedure</button>
        {codeSteps.length > 0 && (
          <select onChange={e => { addCode(e.target.value); e.target.value = '' }} defaultValue="" style={{ ...inp, width: 220 }}>
            <option value="" disabled>+ Add code step…</option>
            {codeSteps.map(cs => <option key={cs.name} value={cs.name}>{cs.name}</option>)}
          </select>
        )}
        <button onClick={() => showQuery ? cancelQuery() : (setEditIdx(null), setQName(''), setQSql(''), setShowQuery(true))} style={btn()}><Plus size={12} /> Add SQL query</button>
      </div>

      {/* SQL query editor — paste a read-only SELECT to export as its own file */}
      {showQuery && (
        <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: 12, marginBottom: 16, background: '#f8fafc' }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: C.textSub, marginBottom: 8 }}>
            {editIdx != null ? 'Edit SQL query' : 'New SQL query'}
          </div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
            <label style={{ ...lbl, margin: 0, minWidth: 90 }}>Output name</label>
            <input value={qName} onChange={e => setQName(e.target.value)} placeholder="e.g. daily_sales"
                   style={{ ...inp, width: 240, fontFamily: 'monospace', fontSize: 12 }} />
            <span style={{ fontSize: 11, color: C.textMuted }}>→ saved as &lt;name&gt;.{form.file_format}</span>
          </div>
          <textarea value={qSql} onChange={e => setQSql(e.target.value)} rows={8}
                    placeholder="SELECT ... FROM ...   (read-only; EXEC allowed only for sp_executesql)"
                    style={{ ...inp, width: '100%', fontFamily: 'monospace', fontSize: 12, resize: 'vertical' }} />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 8 }}>
            <button onClick={cancelQuery} style={btn()}>Cancel</button>
            <button onClick={submitQuery} style={btn(C.primary, '#fff')}>
              {editIdx != null ? <><Pencil size={12} /> Update query step</> : <><Plus size={12} /> Add query step</>}
            </button>
          </div>
        </div>
      )}

      {/* Stored-procedure params editor */}
      {procEditIdx != null && form.steps[procEditIdx] && (
        <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: 12, marginBottom: 16, background: '#f8fafc' }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: C.textSub, marginBottom: 8 }}>
            Procedure: <span style={{ fontFamily: 'monospace' }}>{form.steps[procEditIdx].name}</span>
          </div>
          <div style={{ display: 'flex', gap: 8, marginBottom: 10, alignItems: 'center' }}>
            <label style={{ ...lbl, margin: 0, minWidth: 90 }}>Output name</label>
            <input value={procOutName} onChange={e => setProcOutName(e.target.value)} placeholder="(defaults to proc name)"
                   style={{ ...inp, width: 240, fontFamily: 'monospace', fontSize: 12 }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
            <label style={{ ...lbl, margin: 0 }}>Parameters (variables)</label>
            <button onClick={loadProcParams} disabled={procLoading} style={btn()}>
              <RefreshCw size={12} className={procLoading ? 'spin' : ''} /> Load from procedure
            </button>
          </div>
          <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 8 }}>
            Leave a value blank to pass <b>NULL</b> — fill only the parameters you want, the rest go as NULL.
          </div>
          {procParams.length === 0 && (
            <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 8 }}>
              No parameters. Click “Load from procedure” to pull its @variables, or add them manually.
            </div>
          )}
          {procParams.map((p, i) => (
            <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 6, alignItems: 'center' }}>
              <span style={{ fontFamily: 'monospace', fontSize: 12, color: C.textSub }}>@</span>
              <input value={p.name} onChange={e => setProcParam(i, 'name', e.target.value)} placeholder="param name"
                     style={{ ...inp, width: 200, fontFamily: 'monospace', fontSize: 12 }} />
              <span style={{ color: C.textMuted }}>=</span>
              <input value={p.value} onChange={e => setProcParam(i, 'value', e.target.value)} placeholder="blank = NULL"
                     style={{ ...inp, width: 220, fontSize: 12 }} />
              {p.type && <span style={{ fontSize: 10, color: C.textMuted }}>{p.type}</span>}
              <button onClick={() => removeProcParam(i)} style={iconBtn(C.textMuted)}><X size={13} /></button>
            </div>
          ))}
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8 }}>
            <button onClick={addProcParam} style={btn()}><Plus size={12} /> Add parameter</button>
            <div style={{ display: 'flex', gap: 8 }}>
              <button onClick={cancelProc} style={btn()}>Cancel</button>
              <button onClick={saveProc} style={btn(C.primary, '#fff')}><Pencil size={12} /> Save parameters</button>
            </div>
          </div>
        </div>
      )}

      {/* Deliver to — combine any of folder / snowflake / email */}
      <label style={lbl}>Deliver to</label>
      <div style={{ display: 'flex', gap: 10, marginBottom: 6, flexWrap: 'wrap' }}>
        <DeliverChip active={dFolder} icon={<FolderOutput size={14} />} label="Folder"
          onClick={() => setDeliver(!dFolder, dSnow)} />
        <DeliverChip active={dSnow} icon={<Database size={14} />} label="Snowflake"
          onClick={() => setDeliver(dFolder, !dSnow)} />
        <DeliverChip active={dEmail} icon={<Mail size={14} />} label="Email"
          onClick={() => setEmail('enabled', !dEmail)} />
      </div>
      {!dFolder && !dSnow && dEmail && (
        <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 12 }}>
          Email only — files are generated just to build the attachment, not kept.
        </div>
      )}
      {!dFolder && !dSnow && !dEmail && (
        <div style={{ fontSize: 11, color: C.amber, marginBottom: 12 }}>
          Pick at least one destination.
        </div>
      )}
      <div style={{ height: 8 }} />

      {/* Folder */}
      {dFolder && (
        <div style={{ marginBottom: 16 }}>
          <Field label="Export folder (optional — defaults to backend/exports)">
            <input value={form.base_dir} onChange={e => set('base_dir', e.target.value)}
                   placeholder="D:\exports" style={{ ...inp, fontFamily: 'monospace' }} />
          </Field>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: C.textSub, cursor: 'pointer', marginTop: 8 }}>
            <input type="checkbox" checked={!!form.folder_per_run}
                   onChange={e => set('folder_per_run', e.target.checked)}
                   style={{ width: 14, height: 14, cursor: 'pointer' }} />
            Create a new sub-folder for each run
          </label>
          <div style={{ fontSize: 11, color: C.textMuted, marginTop: 4 }}>
            {form.folder_per_run
              ? <>Files go to &lt;folder&gt;\data\&lt;YYYYMMDD&gt;\&lt;session_code&gt;\ — a fresh folder each run, grouped by date.</>
              : <>Files go to &lt;folder&gt;\data\&lt;YYYYMMDD&gt;\ — one folder per day, reused (overwrites same-named files).</>}
          </div>
        </div>
      )}

      {dSnow && (
        <div style={{ marginBottom: 16 }}>
          <label style={lbl}>Snowflake sync</label>
          <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: 12, background: '#f8fafc' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 12 }}>
              <div>
                <label style={lbl}>Database</label>
                <input value={sf.database || ''} onChange={e => setSnow('database', e.target.value)}
                       placeholder="ANALYTICS" style={{ ...inp, fontSize: 12 }} />
              </div>
              <div>
                <label style={lbl}>Schema</label>
                <input value={sf.schema || ''} onChange={e => setSnow('schema', e.target.value)}
                       placeholder="ARS" style={{ ...inp, fontSize: 12 }} />
              </div>
              <div>
                <label style={lbl}>Warehouse (optional)</label>
                <input value={sf.warehouse || ''} onChange={e => setSnow('warehouse', e.target.value)}
                       placeholder="uses default" style={{ ...inp, fontSize: 12 }} />
              </div>
            </div>
            <label style={lbl}>Targets — one per file to upsert</label>
            {sfTargets.length === 0 && (
              <div style={{ fontSize: 11, color: C.textMuted, margin: '2px 0 8px' }}>No targets yet.</div>
            )}
            {sfTargets.map((t, i) => (
              <div key={i} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1.2fr 1fr auto', gap: 8, marginBottom: 8, alignItems: 'center' }}>
                <input value={t.source_file || ''} onChange={e => setTarget(i, 'source_file', e.target.value)}
                       placeholder="source file/proc" title="Which step's output (proc or query name)"
                       style={{ ...inp, fontSize: 11, fontFamily: 'monospace' }} />
                <input value={t.table || ''} onChange={e => setTarget(i, 'table', e.target.value)}
                       placeholder="target table" style={{ ...inp, fontSize: 11, fontFamily: 'monospace' }} />
                <input value={(t.key_cols || []).join(', ')} onChange={e => setTarget(i, 'key_cols', e.target.value.split(',').map(c => c.trim()).filter(Boolean))}
                       placeholder="key cols (merge on)" style={{ ...inp, fontSize: 11, fontFamily: 'monospace' }} />
                <input value={t.watermark_col || ''} onChange={e => setTarget(i, 'watermark_col', e.target.value)}
                       placeholder="watermark col" title="Column tracking new/changed rows (incremental)"
                       style={{ ...inp, fontSize: 11, fontFamily: 'monospace' }} />
                <button onClick={() => removeTarget(i)} style={iconBtn(C.textMuted)}><X size={13} /></button>
              </div>
            ))}
            <button onClick={addTarget} style={btn()}><Plus size={12} /> Add target</button>
            <div style={{ fontSize: 10, color: C.textMuted, marginTop: 8 }}>
              Key cols = merge/upsert keys. Watermark col (optional) = only rows newer than last sync
              are sent. Connection is configured in Settings → Snowflake.
            </div>
          </div>
        </div>
      )}

      {/* Split output — applies to folder files AND emailed attachments */}
      {(dFolder || dEmail) && (
        <div style={{ marginBottom: 16 }}>
          <label style={{ ...lbl, display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input type="checkbox" checked={!!sp.enabled}
                   onChange={e => setSplit('enabled', e.target.checked)}
                   style={{ width: 14, height: 14, cursor: 'pointer' }} />
            Split output into multiple files{dEmail && !dFolder ? ' (splits the email attachments)' : ''}
          </label>
          {sp.enabled && (
            <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: 12, marginTop: 6, background: '#f8fafc' }}>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 10 }}>
                <div>
                  <label style={lbl}>Split by</label>
                  <select value={sp.method} onChange={e => setSplit('method', e.target.value)} style={{ ...inp, fontSize: 12 }}>
                    <option value="product">Product hierarchy</option>
                    <option value="store">Store hierarchy</option>
                    <option value="none">Size only</option>
                  </select>
                </div>
                <div>
                  <label style={lbl}>Max rows per file</label>
                  <input type="number" min="1" value={sp.max_rows}
                         onChange={e => setSplit('max_rows', +e.target.value)}
                         style={{ ...inp, fontSize: 12 }} />
                </div>
              </div>
              {sp.method === 'product' && (
                <div>
                  <label style={lbl}>Product hierarchy columns</label>
                  <input value={(sp.product_hierarchy || []).join(', ')}
                         onChange={e => setSplit('product_hierarchy', e.target.value.split(',').map(c => c.trim()).filter(Boolean))}
                         placeholder="SEG, DIV, SUB_DIV, MAJ_CAT"
                         style={{ ...inp, fontSize: 12, fontFamily: 'monospace' }} />
                </div>
              )}
              {sp.method === 'store' && (
                <div>
                  <label style={lbl}>Store hierarchy columns</label>
                  <input value={(sp.store_hierarchy || []).join(', ')}
                         onChange={e => setSplit('store_hierarchy', e.target.value.split(',').map(c => c.trim()).filter(Boolean))}
                         placeholder="ZONE, REG, STORE"
                         style={{ ...inp, fontSize: 12, fontFamily: 'monospace' }} />
                </div>
              )}
              <div style={{ fontSize: 10, color: C.textMuted, marginTop: 6 }}>
                {sp.method === 'none'
                  ? 'One file, split into parts when it exceeds max rows.'
                  : 'One file per group of the chosen columns; a group over max rows is split into _part2, _part3…'}
                {' '}Columns not present in a result set are ignored.
              </div>
            </div>
          )}
        </div>
      )}

      {/* Email delivery (shown when the Email chip above is on) */}
      {dEmail && (
        <div style={{ marginBottom: 16 }}>
          <label style={lbl}>Email settings</label>
          <div style={{ border: `1px solid ${C.cardBorder}`, borderRadius: 8, padding: 12, marginTop: 6, background: '#f8fafc' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 10 }}>
              <div>
                <label style={lbl}>Attach as</label>
                <select value={em.attach_format} onChange={e => setEmail('attach_format', e.target.value)} style={{ ...inp, fontSize: 12 }}>
                  <option value="xlsx">Excel (.xlsx)</option>
                  <option value="pdf">PDF (.pdf)</option>
                  <option value="docx">Word (.docx)</option>
                  <option value="source">Original files (as generated)</option>
                </select>
              </div>
              <div>
                <label style={lbl}>Send</label>
                <select value={em.attach_scope || 'all'} onChange={e => setEmail('attach_scope', e.target.value)} style={{ ...inp, fontSize: 12 }}>
                  <option value="all">All files</option>
                  <option value="manifest">Manifest only</option>
                </select>
              </div>
            </div>
            {em.attach_scope === 'manifest' && (
              <div style={{ fontSize: 10, color: C.textMuted, margin: '-4px 0 10px' }}>
                Add the <code style={{ fontFamily: 'monospace' }}>build_manifest</code> code step so a manifest exists to email.
              </div>
            )}
            <div style={{ marginBottom: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: C.textSub, cursor: 'pointer' }}>
                <input type="checkbox" checked={!!em.zip} onChange={e => setEmail('zip', e.target.checked)}
                       style={{ width: 14, height: 14, cursor: 'pointer' }} />
                Zip attachments into one file
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: C.textSub, cursor: 'pointer' }}>
                <input type="checkbox" checked={!!em.notify_on_fail} onChange={e => setEmail('notify_on_fail', e.target.checked)}
                       style={{ width: 14, height: 14, cursor: 'pointer' }} />
                Also email these recipients if the report fails
              </label>
            </div>
            <div style={{ marginBottom: 10 }}>
              <label style={lbl}>To</label>
              <input value={(em.to || []).join(', ')} onChange={e => setEmail('to', parseAddrs(e.target.value))}
                     placeholder="alice@company.com, bob@company.com" style={{ ...inp, fontSize: 12 }} />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 10 }}>
              <div>
                <label style={lbl}>Cc</label>
                <input value={(em.cc || []).join(', ')} onChange={e => setEmail('cc', parseAddrs(e.target.value))}
                       placeholder="optional" style={{ ...inp, fontSize: 12 }} />
              </div>
              <div>
                <label style={lbl}>Bcc</label>
                <input value={(em.bcc || []).join(', ')} onChange={e => setEmail('bcc', parseAddrs(e.target.value))}
                       placeholder="optional" style={{ ...inp, fontSize: 12 }} />
              </div>
            </div>
            <div style={{ marginBottom: 10 }}>
              <label style={lbl}>Subject</label>
              <input value={em.subject || ''} onChange={e => setEmail('subject', e.target.value)}
                     placeholder="Report: {name}" style={{ ...inp, fontSize: 12 }} />
            </div>
            <div>
              <label style={lbl}>Message</label>
              <textarea value={em.body || ''} onChange={e => setEmail('body', e.target.value)} rows={3}
                        placeholder="Optional email body" style={{ ...inp, fontSize: 12, width: '100%', resize: 'vertical' }} />
            </div>
            <div style={{ fontSize: 10, color: C.textMuted, marginTop: 6 }}>
              Multiple addresses separated by comma. Requires SMTP to be configured on the server.
            </div>
          </div>
        </div>
      )}

      {/* Trigger */}
      <label style={lbl}>When should it generate?</label>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10, marginBottom: 16 }}>
        <TriggerCard active={form.trigger_type === 'schedule'} onClick={() => set('trigger_type', 'schedule')} icon={<Clock size={14} />} title="On a schedule">
          <select value={sc.freq} onChange={e => setSched('freq', e.target.value)} style={{ ...inp, fontSize: 12, marginBottom: 6 }}>
            <option value="once">One time (specific date)</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
            <option value="every_n_hours">Every N hours</option>
          </select>

          {sc.freq === 'once' && (
            <input type="datetime-local" value={sc.datetime || ''}
                   onChange={e => setSched('datetime', e.target.value)}
                   style={{ ...inp, fontSize: 12 }} />
          )}

          {sc.freq === 'every_n_hours' && (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                <span style={{ fontSize: 11, color: C.textSub }}>Every</span>
                <input type="number" min="1" max="24" value={sc.every_n_hours}
                       onChange={e => setSched('every_n_hours', +e.target.value)}
                       style={{ ...inp, fontSize: 12, width: 60 }} />
                <span style={{ fontSize: 11, color: C.textSub }}>hours</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: 11, color: C.textSub }}>from</span>
                <input value={sc.start || ''} onChange={e => setSched('start', e.target.value)}
                       placeholder="12:00" style={{ ...inp, fontSize: 12, width: 70 }} />
                <span style={{ fontSize: 11, color: C.textSub }}>to</span>
                <input value={sc.end || ''} onChange={e => setSched('end', e.target.value)}
                       placeholder="23:00" style={{ ...inp, fontSize: 12, width: 70 }} />
              </div>
              <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4 }}>
                Runs within the window each day. Clear both for round-the-clock. UTC.
              </div>
            </>
          )}

          {sc.freq === 'monthly' && (() => {
            const mdays = sc.days || []
            return (
              <>
                <input value={mdays.join(', ')}
                       onChange={e => setSched('days', e.target.value.split(',').map(d => parseInt(d.trim(), 10)).filter(d => d >= 1 && d <= 31))}
                       placeholder="dates e.g. 1, 15, 28"
                       style={{ ...inp, fontSize: 12, marginBottom: 6 }} />
                <div style={{ fontSize: 10, color: C.textMuted, marginBottom: 6 }}>
                  One or more dates (1–31), comma-separated. 29–31 clamp to month end.
                </div>
              </>
            )
          })()}

          {sc.freq === 'weekly' && (() => {
            const days = sc.weekdays || []
            const toggle = (i) => setSched('weekdays',
              days.includes(i) ? days.filter(d => d !== i) : [...days, i].sort((a, b) => a - b))
            return (
              <div style={{ marginBottom: 6 }}>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d, i) => (
                    <button key={i} onClick={() => toggle(i)} type="button"
                      style={{ padding: '4px 8px', fontSize: 11, borderRadius: 6, cursor: 'pointer',
                               border: `1px solid ${days.includes(i) ? C.primary : C.cardBorder}`,
                               background: days.includes(i) ? C.primary : '#fff',
                               color: days.includes(i) ? '#fff' : C.textSub, fontWeight: 600 }}>{d}</button>
                  ))}
                </div>
                <div style={{ display: 'flex', gap: 8, marginTop: 5 }}>
                  <button type="button" onClick={() => setSched('weekdays', [0, 1, 2, 3, 4])} style={presetBtn}>Mon–Fri</button>
                  <button type="button" onClick={() => setSched('weekdays', [0, 1, 2, 3, 4, 5, 6])} style={presetBtn}>Every day</button>
                  <button type="button" onClick={() => setSched('weekdays', [5, 6])} style={presetBtn}>Weekend</button>
                </div>
              </div>
            )
          })()}

          {['daily', 'weekly', 'monthly'].includes(sc.freq) && (
            <>
              <input value={(sc.times || []).join(', ')}
                     onChange={e => setSched('times', e.target.value.split(',').map(t => t.trim()).filter(Boolean))}
                     placeholder="07:00, 15:00"
                     style={{ ...inp, fontSize: 12 }} />
              <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4 }}>
                One or more times, comma-separated. UTC.
              </div>
            </>
          )}
          {sc.freq === 'once' && (
            <div style={{ fontSize: 10, color: C.textMuted, marginTop: 4 }}>Runs once, then stops. UTC.</div>
          )}
        </TriggerCard>
        <TriggerCard active={form.trigger_type === 'event'} onClick={() => set('trigger_type', 'event')} icon={<Link2 size={14} />} title="When a process completes">
          <select value={form.trigger_event} onChange={e => set('trigger_event', e.target.value)} style={{ ...inp, fontSize: 12 }}>
            {events.map(ev => <option key={ev.event} value={ev.event}>{ev.label}</option>)}
          </select>
        </TriggerCard>
        <TriggerCard active={form.trigger_type === 'manual'} onClick={() => set('trigger_type', 'manual')} icon={<MousePointerClick size={14} />} title="Manual only">
          <div style={{ fontSize: 11, color: C.textMuted }}>Generate from the list.</div>
        </TriggerCard>
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
        <button onClick={onCancel} style={btn()}>Cancel</button>
        <button onClick={onSave} style={btn(C.primary, '#fff')}>{form.report_id ? 'Save changes' : 'Save report'}</button>
      </div>
    </div>
  )
}

const StatCard = ({ icon, label, value, tint, bg, active, onClick }) => (
  <div onClick={onClick} role="button" tabIndex={0}
       onKeyDown={e => { if (onClick && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onClick() } }}
       style={{ background: active ? bg : C.cardBg,
                border: `${active ? 2 : 1}px solid ${active ? tint : C.cardBorder}`, borderRadius: 12,
                padding: active ? '11px 13px' : '12px 14px', display: 'flex', alignItems: 'center', gap: 12,
                cursor: onClick ? 'pointer' : 'default', transition: 'border-color .12s, background .12s' }}>
    <div style={{ width: 34, height: 34, borderRadius: 9, background: bg, color: tint,
                  display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>{icon}</div>
    <div>
      <div style={{ fontSize: 22, fontWeight: 800, lineHeight: 1, color: C.text }}>{value}</div>
      <div style={{ fontSize: 11, color: active ? tint : C.textMuted, marginTop: 3, fontWeight: active ? 600 : 400 }}>{label}</div>
    </div>
  </div>
)

const statusPill = (s) => ({
  display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, fontWeight: 600,
  padding: '3px 9px', borderRadius: 20, textTransform: 'capitalize',
  color: statusColor(s),
  background: s === 'completed' ? C.greenBg : s === 'failed' ? C.redBg
    : s === 'running' ? C.primaryLight
    : (s === 'skipped' || s === 'cancelled') ? C.amberBg : C.grayBg,
})

const DeliverChip = ({ active, icon, label, onClick }) => (
  <button type="button" onClick={onClick}
    style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '7px 14px',
             borderRadius: 8, cursor: 'pointer', fontSize: 13, fontWeight: 600,
             border: `${active ? 2 : 1}px solid ${active ? C.primary : C.cardBorder}`,
             background: active ? C.primaryLight : '#fff',
             color: active ? C.primary : C.textSub }}>
    {icon}{label}
    {active && <CheckCircle size={13} />}
  </button>
)

const Badge = ({ icon, text, bg, fg }) => (
  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 11, fontWeight: 600,
                 padding: '3px 9px', borderRadius: 6, background: bg, color: fg }}>{icon}{text}</span>
)
const Field = ({ label, children }) => (
  <div><label style={lbl}>{label}</label>{children}</div>
)
const TriggerCard = ({ active, onClick, icon, title, children }) => (
  <div onClick={onClick} style={{ cursor: 'pointer', border: `${active ? 2 : 1}px solid ${active ? C.primary : C.cardBorder}`,
                                  borderRadius: 8, padding: 12 }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 700, marginBottom: 8,
                  color: active ? C.text : C.textSub }}>{icon}{title}</div>
    {children}
  </div>
)

const th = { padding: '10px 12px', textAlign: 'left', fontWeight: 700, fontSize: 11, color: C.textSub, textTransform: 'uppercase', letterSpacing: 0.4 }
const td = { padding: '10px 12px', verticalAlign: 'middle' }
const lbl = { display: 'block', fontSize: 12, color: C.textSub, marginBottom: 4, fontWeight: 600 }
const inp = { width: '100%', padding: '7px 10px', borderRadius: 6, border: `1px solid ${C.cardBorder}`, fontSize: 13, outline: 'none', background: '#fff', color: C.text, boxSizing: 'border-box' }
const iconBtn = (color) => ({ background: 'none', border: 'none', cursor: 'pointer', padding: 4, color, display: 'inline-flex', alignItems: 'center', borderRadius: 4 })
const btn = (bg = '#fff', fg = C.textSub) => ({ padding: '7px 14px', borderRadius: 6, fontSize: 12, fontWeight: 600,
  border: `1px solid ${bg === '#fff' ? C.cardBorder : bg}`, background: bg, color: fg, cursor: 'pointer',
  display: 'inline-flex', alignItems: 'center', gap: 5 })

function fmtTime(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) }
  catch { return iso }
}

function fmtDuration(ms) {
  if (ms == null || ms === '') return '—'
  const n = Number(ms)
  if (Number.isNaN(n)) return '—'
  if (n < 1000) return `${Math.round(n)} ms`
  if (n < 60000) return `${(n / 1000).toFixed(1)}s`
  const m = Math.floor(n / 60000)
  const s = Math.round((n % 60000) / 1000)
  return `${m}m ${s}s`
}
