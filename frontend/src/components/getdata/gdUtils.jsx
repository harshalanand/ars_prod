// Shared bits for the Get Data module (pages under src/pages/getdata/).
// Times from the API are UTC ISO strings ending in 'Z'; everything is shown in IST.
import { CheckCircle, XCircle, Clock, MinusCircle, AlertTriangle } from 'lucide-react'
import { Link } from 'react-router-dom'
import useAuthStore from '@/store/authStore'

const IST = 'Asia/Kolkata'

export const fmtNum = (n) => (n == null || n === '' ? '—' : Number(n).toLocaleString('en-IN'))

// 12,40,512 → "12.4 L"; 3,10,00,000 → "3.1 Cr"
export const fmtCompact = (n) => {
  if (n == null) return '—'
  const v = Number(n)
  if (Math.abs(v) >= 1e7) return `${(v / 1e7).toFixed(1)} Cr`
  if (Math.abs(v) >= 1e5) return `${(v / 1e5).toFixed(1)} L`
  return v.toLocaleString('en-IN')
}

export const fmtIST = (iso, withYear = false) => {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d)) return '—'
  return d.toLocaleString('en-IN', {
    timeZone: IST, day: '2-digit', month: 'short', ...(withYear ? { year: 'numeric' } : {}),
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

export const istToday = () => new Date().toLocaleDateString('en-CA', { timeZone: IST })   // YYYY-MM-DD
export const istDaysAgo = (n) => {
  const d = new Date(Date.now() - n * 86400000)
  return d.toLocaleDateString('en-CA', { timeZone: IST })
}

export const fmtDur = (ms) => {
  if (ms == null) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}

export const MODE_LABEL = { replace: 'Full replace', incremental: 'Incremental', append: 'Append' }

const WD = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

export function scheduleText(job) {
  if (!job || job.TRIGGER_TYPE !== 'schedule' || !job.SCHEDULE_CONFIG) return 'Manual only'
  const c = job.SCHEDULE_CONFIG
  const times = (c.times || []).join(', ')
  if (c.freq === 'every_n_hours') return `Every ${c.every_n_hours} h${c.start && c.end ? ` · ${c.start}–${c.end}` : ''}`
  if (c.freq === 'weekly') return `${(c.weekdays || []).map(w => WD[w]).join(', ')} ${times}`
  if (c.freq === 'monthly') return `Day ${(c.days || []).join(', ')} · ${times}`
  return `Daily ${times}`
}

const PILL = {
  success: ['bg-green-100 text-green-700', CheckCircle],
  failed: ['bg-red-100 text-red-700', XCircle],
  running: ['bg-amber-100 text-amber-700', Clock],
  skipped: ['bg-gray-100 text-gray-500', MinusCircle],
}

export function StatusPill({ status, children }) {
  const [cls, Icon] = PILL[String(status || '').toLowerCase()] || ['bg-gray-100 text-gray-500', null]
  return (
    <span className={`inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full whitespace-nowrap ${cls}`}>
      {Icon && <Icon size={11} className={status === 'running' ? 'animate-pulse' : ''} />}
      {children ?? status}
    </span>
  )
}

export function RunTypePill({ type }) {
  const auto = type === 'AUTO'
  return (
    <span className={`inline-block text-[10px] font-semibold tracking-wide px-2 py-0.5 rounded-full ${
      auto ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600 border border-gray-200'}`}>
      {auto ? 'AUTO' : 'MANUAL'}
    </span>
  )
}

export function useGetDataPerms() {
  const isSuper = useAuthStore(s => s.isSuperAdmin?.())
  const isAdmin = useAuthStore(s => s.hasRole?.('ADMIN'))
  const manage = useAuthStore(s => s.hasPermission?.('GET_DATA_MANAGE'))
  const run = useAuthStore(s => s.hasPermission?.('GET_DATA_RUN'))
  const canManage = !!(isSuper || isAdmin || manage)
  return { canManage, canRun: canManage || !!run }
}

// Shown on every Snowflake page when the shared connection can't be used.
export function SnowflakeBanner({ status }) {
  if (!status || status.enabled) return null
  return (
    <div className="flex items-start gap-2 text-sm px-4 py-3 rounded-lg bg-amber-50 border border-amber-200 text-amber-800">
      <AlertTriangle size={16} className="mt-0.5 shrink-0" />
      <div>
        Snowflake is turned off, so views can't be created and sync jobs will fail.
        {' '}<Link to="/settings?tab=snowflake" className="underline font-medium">Turn it on in Settings → Snowflake</Link>.
      </div>
    </div>
  )
}

export const errText = (e, fallback = 'Something went wrong') =>
  e?.response?.data?.detail || e?.message || fallback

// Compact result grid for Snowflake previews: { columns, column_meta, rows: [[...]] }
export function PreviewGrid({ data, maxHeight = 360 }) {
  if (!data) return null
  const meta = data.column_meta || []
  return (
    <div className="border border-gray-200 rounded-lg overflow-auto" style={{ maxHeight }}>
      <table className="text-xs min-w-full">
        <thead className="bg-gray-50 sticky top-0 z-10">
          <tr>
            {data.columns.map((c, i) => (
              <th key={c + i} className="px-3 py-1.5 text-left font-semibold text-gray-600 whitespace-nowrap border-b"
                  title={meta[i] ? `${meta[i].sf_type_label} → ${meta[i].sql_type}` : ''}>
                {c}
                {meta[i] && <div className="font-normal text-[10px] text-gray-400">{meta[i].sf_type_label}</div>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {data.rows.map((r, i) => (
            <tr key={i} className="hover:bg-gray-50">
              {r.map((v, j) => (
                <td key={j} className="px-3 py-1 font-mono whitespace-nowrap text-gray-700">
                  {v === null ? <span className="text-gray-300">null</span> : String(v)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {data.rows.length === 0 && <div className="p-4 text-center text-sm text-gray-400">The query returned no rows.</div>}
    </div>
  )
}

// Validation / test checklist: [{ label, ok, detail }]
export function CheckList({ checks }) {
  if (!checks?.length) return null
  return (
    <ul className="grid sm:grid-cols-2 gap-x-4 gap-y-1 text-xs">
      {checks.map((c, i) => (
        <li key={i} className={`flex items-start gap-1.5 ${c.ok ? 'text-green-700' : 'text-red-600'}`}>
          {c.ok ? <CheckCircle size={13} className="mt-0.5 shrink-0" /> : <XCircle size={13} className="mt-0.5 shrink-0" />}
          <span><span className="font-medium">{c.label}</span>{c.detail ? <span className="text-gray-500"> · {c.detail}</span> : null}</span>
        </li>
      ))}
    </ul>
  )
}
