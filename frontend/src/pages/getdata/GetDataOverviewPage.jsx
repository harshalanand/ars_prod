import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  DatabaseZap, Snowflake, FileSpreadsheet, PlugZap, Server, RefreshCw, CalendarClock,
  AlertTriangle, ArrowRight,
} from 'lucide-react'
import { getDataAPI } from '@/services/api'
import {
  fmtNum, fmtCompact, fmtIST, RunTypePill, SnowflakeBanner, errText,
} from '@/components/getdata/gdUtils'

const SOURCE_UI = {
  snowflake: { icon: Snowflake, to: '/get-data/snowflake/jobs', blurb: 'Views → local SQL tables' },
  excel: { icon: FileSpreadsheet, to: '/get-data/excel', blurb: 'Upload or folder pickup' },
  datav2: { icon: PlugZap, to: '/get-data/datav2', blurb: 'Source to be confirmed' },
  sap: { icon: Server, to: '/get-data/sap', blurb: 'Will absorb SAP Data Pulls' },
}

function Metric({ label, value, tone }) {
  const color = tone === 'good' ? 'text-green-700' : tone === 'bad' ? 'text-red-600' : 'text-gray-900'
  return (
    <div className="bg-gray-50 rounded-lg px-4 py-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`text-2xl font-semibold mt-0.5 ${color}`}>{value}</div>
    </div>
  )
}

export default function GetDataOverviewPage() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const timer = useRef(null)

  const load = async (quiet = false) => {
    if (!quiet) setLoading(true)
    try {
      const { data: res } = await getDataAPI.overview()
      setData(res.data)
      setError(null)
    } catch (e) {
      setError(errText(e, 'Could not load the overview'))
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])
  // Refresh often while something is running, rarely otherwise.
  useEffect(() => {
    clearTimeout(timer.current)
    const busy = (data?.jobs?.running || 0) > 0
    timer.current = setTimeout(() => load(true), busy ? 5000 : 30000)
    return () => clearTimeout(timer.current)
  }, [data])

  const t = data?.today || {}
  const j = data?.jobs || {}

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><DatabaseZap size={24} /> Get Data</h1>
          <p className="text-gray-500 text-sm mt-0.5">Bring data into local SQL (Rep_data) from outside sources, on a schedule or on demand. Times are IST.</p>
        </div>
        <button onClick={() => load()} className="btn-secondary"><RefreshCw size={16} className={loading ? 'animate-spin' : ''} /> Refresh</button>
      </div>

      {error && <div className="card p-4 text-sm text-red-600">{error}</div>}
      <SnowflakeBanner status={data?.snowflake} />

      {/* Sources */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {(data?.sources || []).map(s => {
          const ui = SOURCE_UI[s.key] || {}
          const Icon = ui.icon || DatabaseZap
          const active = s.status === 'active'
          return (
            <Link key={s.key} to={ui.to || '#'}
              className={`card p-4 hover:border-primary-300 transition ${active ? 'border-primary-300' : ''}`}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 font-semibold text-gray-900"><Icon size={18} /> {s.label}</div>
                {active
                  ? <span className="text-[11px] px-2 py-0.5 rounded-full bg-green-100 text-green-700">active</span>
                  : <span className="text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500">phase {s.phase}</span>}
              </div>
              <div className="text-xs text-gray-500 mt-1">{ui.blurb}</div>
              {active && (
                <div className="text-xs text-gray-600 mt-2">
                  {fmtNum(j.total)} job(s) · {fmtNum(j.enabled)} enabled · {fmtNum(j.scheduled)} scheduled
                  {j.running ? <span className="text-amber-700"> · {j.running} running</span> : null}
                </div>
              )}
            </Link>
          )
        })}
      </div>

      {/* Today */}
      <div>
        <div className="text-sm font-semibold text-gray-700 mb-2">Today</div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <Metric label="Runs" value={fmtNum(t.runs)} />
          <Metric label="Auto · manual" value={`${fmtNum(t.auto_runs)} · ${fmtNum(t.manual_runs)}`} />
          <Metric label="Success" value={fmtNum(t.success)} tone="good" />
          <Metric label="Failed" value={fmtNum(t.failed)} tone={t.failed ? 'bad' : undefined} />
          <Metric label="Rows loaded" value={fmtCompact(t.rows_loaded)} />
        </div>
      </div>

      <div className="grid lg:grid-cols-2 gap-4">
        <div className="card p-0 overflow-hidden">
          <div className="px-4 py-2.5 border-b text-sm font-semibold text-gray-700 flex items-center gap-2"><CalendarClock size={15} /> Next scheduled runs</div>
          {(data?.next_runs || []).length === 0
            ? <div className="p-6 text-sm text-gray-400 text-center">No scheduled jobs. <Link to="/get-data/snowflake/jobs" className="text-primary-600 underline">Add a schedule</Link> to a sync job.</div>
            : <ul className="divide-y divide-gray-100">
                {data.next_runs.map(r => {
                  const retryFirst = r.RETRY_AT && (!r.NEXT_RUN_AT || r.RETRY_AT < r.NEXT_RUN_AT)
                  return (
                    <li key={r.JOB_ID} className="px-4 py-2 flex items-center justify-between text-sm">
                      <div>
                        <div className="font-medium text-gray-900">{r.JOB_NAME}</div>
                        <div className="text-xs font-mono text-gray-400">{r.TARGET_TABLE}</div>
                      </div>
                      <div className="text-right text-xs">
                        <div className="text-gray-700">{fmtIST(retryFirst ? r.RETRY_AT : r.NEXT_RUN_AT)}</div>
                        {retryFirst && <div className="text-amber-700">retry after a failure</div>}
                      </div>
                    </li>
                  )
                })}
              </ul>}
        </div>

        <div className="card p-0 overflow-hidden">
          <div className="px-4 py-2.5 border-b text-sm font-semibold text-gray-700 flex items-center gap-2"><AlertTriangle size={15} /> Failures in the last 7 days</div>
          {(data?.recent_failures || []).length === 0
            ? <div className="p-6 text-sm text-gray-400 text-center">No failures.</div>
            : <ul className="divide-y divide-gray-100">
                {data.recent_failures.map(r => (
                  <li key={r.RUN_ID} className="px-4 py-2 text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-medium text-gray-900">#{r.RUN_ID} · {r.JOB_NAME}</div>
                      <div className="flex items-center gap-2 text-xs text-gray-500"><RunTypePill type={r.RUN_TYPE} /> {fmtIST(r.STARTED_AT)}</div>
                    </div>
                    <div className="text-xs text-red-600 mt-0.5 line-clamp-2" title={r.MESSAGE || ''}>{r.MESSAGE}</div>
                  </li>
                ))}
                <li className="px-4 py-2 text-right">
                  <Link to="/get-data/runs?status=failed" className="text-xs text-primary-600 inline-flex items-center gap-1">All failed runs <ArrowRight size={12} /></Link>
                </li>
              </ul>}
        </div>
      </div>

      {(data?.last_7_days || []).length > 0 && (
        <div className="card p-0 overflow-hidden">
          <div className="px-4 py-2.5 border-b text-sm font-semibold text-gray-700">Last 7 days</div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-600 text-xs uppercase">
                <tr>
                  <th className="px-4 py-2 text-left">Day (IST)</th>
                  <th className="px-4 py-2 text-right">Auto</th>
                  <th className="px-4 py-2 text-right">Manual</th>
                  <th className="px-4 py-2 text-right">Failed</th>
                  <th className="px-4 py-2 text-right">Rows loaded</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {data.last_7_days.map(d => (
                  <tr key={d.day}>
                    <td className="px-4 py-2">{new Date(d.day + 'T00:00:00').toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short' })}</td>
                    <td className="px-4 py-2 text-right font-mono">{fmtNum(d.auto_runs)}</td>
                    <td className="px-4 py-2 text-right font-mono">{fmtNum(d.manual_runs)}</td>
                    <td className={`px-4 py-2 text-right font-mono ${d.failed ? 'text-red-600' : ''}`}>{fmtNum(d.failed)}</td>
                    <td className="px-4 py-2 text-right font-mono">{fmtNum(d.rows_loaded)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
