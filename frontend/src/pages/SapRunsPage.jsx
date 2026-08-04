import { useEffect, useState } from 'react'
import { History, RefreshCw, CheckCircle, XCircle, Clock } from 'lucide-react'
import { sapAPI } from '@/services/api'
import toast from 'react-hot-toast'

const pill = (s) => {
  const v = String(s || '').toLowerCase()
  if (v === 'success') return 'bg-green-100 text-green-700'
  if (v === 'failed') return 'bg-red-100 text-red-700'
  if (v === 'running') return 'bg-amber-100 text-amber-700'
  return 'bg-gray-100 text-gray-500'
}

export default function SapRunsPage() {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try {
      const { data } = await sapAPI.listRuns({ limit: 100 })
      setItems(data?.data?.items || [])
    } catch (e) {
      toast.error(e.response?.data?.detail || 'Failed to load runs')
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2"><History size={24} /> SAP Run History</h1>
          <p className="text-gray-500 text-sm mt-0.5">Every pull execution — scheduled or manual — with rows landed and any error.</p>
        </div>
        <button onClick={load} className="btn-secondary"><RefreshCw size={16} className={loading ? 'animate-spin' : ''} /> Refresh</button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-64"><RefreshCw className="animate-spin text-primary-600" size={32} /></div>
      ) : items.length === 0 ? (
        <div className="card p-12 text-center text-gray-500">
          <History size={32} className="mx-auto mb-2 text-gray-300" /> No runs yet.
        </div>
      ) : (
        <div className="card p-0 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-600 text-xs uppercase">
                <tr>
                  <th className="px-4 py-2 text-left">Pull</th>
                  <th className="px-4 py-2 text-left">Trigger</th>
                  <th className="px-4 py-2 text-left">Status</th>
                  <th className="px-4 py-2 text-right">Rows</th>
                  <th className="px-4 py-2 text-left">Target</th>
                  <th className="px-4 py-2 text-right">Duration</th>
                  <th className="px-4 py-2 text-left">When</th>
                  <th className="px-4 py-2 text-left">Message</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {items.map(r => (
                  <tr key={r.RUN_ID} className="hover:bg-gray-50 align-top">
                    <td className="px-4 py-2 font-medium text-gray-900">{r.PULL_NAME || `#${r.PULL_ID}`}</td>
                    <td className="px-4 py-2 text-xs text-gray-500">{r.TRIGGER_SOURCE}</td>
                    <td className="px-4 py-2">
                      <span className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded ${pill(r.STATUS)}`}>
                        {r.STATUS === 'success' ? <CheckCircle size={11} /> : r.STATUS === 'failed' ? <XCircle size={11} /> : <Clock size={11} />}
                        {r.STATUS}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right font-mono">{r.ROWS_FETCHED ?? '—'}</td>
                    <td className="px-4 py-2 font-mono text-xs">{r.TARGET_TABLE || '—'}</td>
                    <td className="px-4 py-2 text-right text-xs text-gray-500">{r.DURATION_MS != null ? `${(r.DURATION_MS / 1000).toFixed(1)}s` : '—'}</td>
                    <td className="px-4 py-2 text-xs text-gray-500 whitespace-nowrap">{r.STARTED_AT ? new Date(r.STARTED_AT).toLocaleString() : '—'}</td>
                    <td className="px-4 py-2 text-xs text-gray-600 max-w-md truncate" title={r.MESSAGE || ''}>{r.MESSAGE || ''}</td>
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
