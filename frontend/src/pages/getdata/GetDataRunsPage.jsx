import { Fragment, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { History, RefreshCw, Download, ChevronDown, ChevronRight, Copy } from 'lucide-react'
import * as XLSX from 'xlsx'
import toast from 'react-hot-toast'
import { getDataAPI } from '@/services/api'
import {
  fmtNum, fmtCompact, fmtIST, fmtDur, StatusPill, RunTypePill, MODE_LABEL, errText,
  istToday, istDaysAgo,
} from '@/components/getdata/gdUtils'

const PAGE = 200

function Metric({ label, value, tone }) {
  return (
    <div className="bg-gray-50 rounded-lg px-4 py-2.5">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`text-xl font-semibold ${tone === 'bad' ? 'text-red-600' : 'text-gray-900'}`}>{value}</div>
    </div>
  )
}

export default function GetDataRunsPage() {
  const [params] = useSearchParams()
  const [filters, setFilters] = useState({
    date_from: istDaysAgo(6), date_to: istToday(),
    job_id: params.get('job_id') || '', run_type: params.get('run_type') || '',
    status: params.get('status') || '',
  })
  const [jobs, setJobs] = useState([])
  const [data, setData] = useState({ items: [], total: 0, summary: {} })
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(null)
  const timer = useRef(null)

  const query = (extra = {}) => {
    const q = { limit: PAGE, ...extra }
    Object.entries(filters).forEach(([k, v]) => { if (v) q[k] = v })
    return q
  }

  const load = async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const { data: res } = await getDataAPI.listRuns(query())
      setData(res.data)
    } catch (e) {
      if (!quiet) toast.error(errText(e, 'Could not load run history'))
    } finally { setLoading(false) }
  }

  const loadMore = async () => {
    try {
      const { data: res } = await getDataAPI.listRuns(query({ offset: data.items.length }))
      setData(d => ({ ...res.data, items: [...d.items, ...res.data.items] }))
    } catch (e) { toast.error(errText(e)) }
  }

  useEffect(() => {
    getDataAPI.listJobs().then(({ data }) => setJobs(data.data.items || [])).catch(() => {})
  }, [])
  useEffect(() => { load() }, [filters])
  useEffect(() => {
    clearTimeout(timer.current)
    const busy = data.items.some(r => r.STATUS === 'running')
    timer.current = setTimeout(() => load(true), busy ? 4000 : 30000)
    return () => clearTimeout(timer.current)
  }, [data])

  const set = (k, v) => setFilters(f => ({ ...f, [k]: v }))
  const s = data.summary || {}

  const exportXlsx = () => {
    const rows = data.items.map(r => ({
      'Run #': r.RUN_ID, Job: r.JOB_NAME, Trigger: r.RUN_TYPE, 'Run by': r.TRIGGERED_BY,
      Attempt: r.ATTEMPT, 'Full reload': r.FULL_RELOAD ? 'yes' : '', Status: r.STATUS,
      'Started (IST)': fmtIST(r.STARTED_AT, true), 'Completed (IST)': fmtIST(r.COMPLETED_AT, true),
      'Duration (s)': r.DURATION_MS != null ? Math.round(r.DURATION_MS / 1000) : null,
      Mode: MODE_LABEL[r.LOAD_MODE] || r.LOAD_MODE, Loader: r.LOADER, Source: r.SOURCE_OBJECT, 'Local table': r.TARGET_TABLE,
      'Snowflake rows': r.SOURCE_ROWS, 'Rows loaded': r.ROWS_LOADED, Inserted: r.ROWS_INSERTED,
      Updated: r.ROWS_UPDATED, 'Table rows before': r.TARGET_ROWS_BEFORE, 'Table rows after': r.TARGET_ROWS_AFTER,
      'Watermark from': r.WATERMARK_FROM, 'Watermark to': r.WATERMARK_TO, 'Snowflake query id': r.SF_QUERY_ID,
      Message: r.MESSAGE,
    }))
    const ws = XLSX.utils.json_to_sheet(rows)
    const wb = XLSX.utils.book_new()
    XLSX.utils.book_append_sheet(wb, ws, 'Run history')
    XLSX.writeFile(wb, `GetData_RunHistory_${filters.date_from}_to_${filters.date_to}.xlsx`)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><History size={24} /> Run History</h1>
          <p className="text-gray-500 text-sm mt-0.5">Every sync run — AUTO (scheduled) or MANUAL (Run now) — with the rows Snowflake returned and the rows that reached local SQL. Times in IST.</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => load()} className="btn-secondary"><RefreshCw size={16} className={loading ? 'animate-spin' : ''} /> Refresh</button>
          <button onClick={exportXlsx} disabled={!data.items.length} className="btn-secondary"><Download size={16} /> Export</button>
        </div>
      </div>

      <div className="card p-3 flex flex-wrap items-end gap-3">
        <div>
          <label className="label">From</label>
          <input type="date" className="input" value={filters.date_from} max={filters.date_to} onChange={e => set('date_from', e.target.value)} />
        </div>
        <div>
          <label className="label">To</label>
          <input type="date" className="input" value={filters.date_to} min={filters.date_from} onChange={e => set('date_to', e.target.value)} />
        </div>
        <div className="min-w-[180px]">
          <label className="label">Job</label>
          <select className="input" value={filters.job_id} onChange={e => set('job_id', e.target.value)}>
            <option value="">All jobs</option>
            {jobs.map(j => <option key={j.JOB_ID} value={j.JOB_ID}>{j.JOB_NAME}</option>)}
          </select>
        </div>
        <div>
          <label className="label">Trigger</label>
          <div className="flex border border-gray-300 rounded-md overflow-hidden text-xs">
            {[['', 'All'], ['AUTO', 'Auto'], ['MANUAL', 'Manual']].map(([k, l]) => (
              <button key={k} onClick={() => set('run_type', k)}
                className={`px-3 py-1 ${filters.run_type === k ? 'bg-primary-50 text-primary-700 font-semibold' : 'bg-white text-gray-600 hover:bg-gray-50'}`}>{l}</button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">Status</label>
          <select className="input" value={filters.status} onChange={e => set('status', e.target.value)}>
            <option value="">All</option>
            <option value="success">Success</option>
            <option value="failed">Failed</option>
            <option value="running">Running</option>
            <option value="skipped">Skipped</option>
          </select>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Metric label="Auto runs" value={fmtNum(s.auto_runs)} />
        <Metric label="Manual runs" value={fmtNum(s.manual_runs)} />
        <Metric label="Rows loaded" value={fmtCompact(s.rows_loaded)} />
        <Metric label="Failed" value={fmtNum(s.failed)} tone={s.failed ? 'bad' : undefined} />
        <Metric label="Skipped" value={fmtNum(s.skipped)} />
      </div>

      {!loading && data.items.length === 0 ? (
        <div className="card p-12 text-center text-gray-500 text-sm">
          <History size={32} className="mx-auto mb-2 text-gray-300" /> No runs match these filters.
        </div>
      ) : (
        <div className="card p-0 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-600 text-xs uppercase">
                <tr>
                  <th className="px-3 py-2 w-6" />
                  <th className="px-3 py-2 text-left">Run</th>
                  <th className="px-3 py-2 text-left">Job</th>
                  <th className="px-3 py-2 text-left">Trigger</th>
                  <th className="px-3 py-2 text-left">Started (IST)</th>
                  <th className="px-3 py-2 text-right">Time</th>
                  <th className="px-3 py-2 text-right">Snowflake rows</th>
                  <th className="px-3 py-2 text-right">Rows loaded</th>
                  <th className="px-3 py-2 text-left">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {data.items.map(r => {
                  const mismatch = r.STATUS !== 'running' && r.SOURCE_ROWS != null && r.ROWS_LOADED != null && r.SOURCE_ROWS !== r.ROWS_LOADED
                  const isOpen = open === r.RUN_ID
                  return (
                    <Fragment key={r.RUN_ID}>
                      <tr onClick={() => setOpen(isOpen ? null : r.RUN_ID)} className="hover:bg-gray-50 cursor-pointer align-top">
                        <td className="px-3 py-2 text-gray-400">{isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
                        <td className="px-3 py-2 font-mono text-xs">#{r.RUN_ID}</td>
                        <td className="px-3 py-2">
                          <div className="font-medium text-gray-900">{r.JOB_NAME || `Job ${r.JOB_ID}`}</div>
                          <div className="text-xs font-mono text-gray-400">{r.TARGET_TABLE}</div>
                        </td>
                        <td className="px-3 py-2">
                          <RunTypePill type={r.RUN_TYPE} />
                          <div className="text-[11px] text-gray-500 mt-0.5">
                            {r.TRIGGERED_BY}{r.ATTEMPT > 1 ? ' · retry' : ''}{r.FULL_RELOAD ? ' · full reload' : ''}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-xs text-gray-600 whitespace-nowrap">{fmtIST(r.STARTED_AT)}</td>
                        <td className="px-3 py-2 text-xs text-gray-600 text-right whitespace-nowrap">{r.STATUS === 'running' ? '—' : fmtDur(r.DURATION_MS)}</td>
                        <td className="px-3 py-2 text-right font-mono text-xs">{fmtNum(r.SOURCE_ROWS)}</td>
                        <td className={`px-3 py-2 text-right font-mono text-xs ${mismatch ? 'text-red-600 font-semibold' : ''}`}>{fmtNum(r.ROWS_LOADED)}</td>
                        <td className="px-3 py-2">
                          <StatusPill status={r.STATUS} />
                          {r.STATUS === 'running' && r.STEP && <div className="text-[11px] text-gray-500 mt-0.5">{r.STEP}</div>}
                        </td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-gray-50/70">
                          <td />
                          <td colSpan={8} className="px-3 py-3"><RunDetail r={r} /></td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
          {data.items.length < data.total && (
            <div className="p-3 text-center border-t">
              <button onClick={loadMore} className="btn-secondary">Load more ({fmtNum(data.total - data.items.length)} left)</button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function RunDetail({ r }) {
  const copy = (t) => { navigator.clipboard?.writeText(t); toast.success('Copied') }
  const rows = [
    ['Mode', MODE_LABEL[r.LOAD_MODE] || r.LOAD_MODE || '—'],
    ['Loader', r.LOADER === 'bulk' ? 'Bulk copy (bcp)' : r.LOADER === 'classic' ? 'Classic insert' : '—'],
    ['Source', r.SOURCE_OBJECT || '—'],
    ['Local table', r.TARGET_TABLE || '—'],
    ['Inserted · updated', r.ROWS_INSERTED != null ? `${fmtNum(r.ROWS_INSERTED)} · ${fmtNum(r.ROWS_UPDATED)}` : '—'],
    ['Table rows before → after', `${fmtNum(r.TARGET_ROWS_BEFORE)} → ${fmtNum(r.TARGET_ROWS_AFTER)}`],
    ['Watermark', r.WATERMARK_FROM || r.WATERMARK_TO ? `${r.WATERMARK_FROM || 'start'} → ${r.WATERMARK_TO || '—'}` : '—'],
    ['Completed (IST)', fmtIST(r.COMPLETED_AT, true)],
  ]
  return (
    <div className="space-y-2 text-xs">
      {r.MESSAGE && (
        <div className={`rounded px-3 py-2 ${r.STATUS === 'failed' ? 'bg-red-50 text-red-700 border border-red-200' : 'bg-white border border-gray-200 text-gray-700'}`}>
          {r.MESSAGE}
        </div>
      )}
      <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-x-6 gap-y-1">
        {rows.map(([k, v]) => (
          <div key={k}><span className="text-gray-500">{k}: </span><span className="font-mono text-gray-800 break-all">{v}</span></div>
        ))}
        {r.SF_QUERY_ID && (
          <div>
            <span className="text-gray-500">Snowflake query id: </span>
            <button onClick={(e) => { e.stopPropagation(); copy(r.SF_QUERY_ID) }} className="font-mono text-primary-700 inline-flex items-center gap-1">
              {r.SF_QUERY_ID.slice(0, 18)}… <Copy size={11} />
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
