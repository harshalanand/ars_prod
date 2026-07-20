/**
 * ManualGalleryPage — the global screenshot directory.
 * Every annotated screenshot across all modules in one searchable grid,
 * grouped by module. Route /manual/gallery.
 */
import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { LayoutGrid, Search, Maximize2, ArrowLeft } from 'lucide-react'
import { MODULES, STEPS, stepImg } from '@/pages/guide/guideSteps'

export default function ManualGalleryPage() {
  const navigate = useNavigate()
  const [q, setQ] = useState('')

  const groups = useMemo(() => {
    const query = q.trim().toLowerCase()
    return MODULES.map(m => {
      const steps = (STEPS[m.id] || []).filter(s =>
        !query || s.title.toLowerCase().includes(query) || m.title.toLowerCase().includes(query) || (s.comment || '').toLowerCase().includes(query))
      return { mod: m, steps }
    }).filter(g => g.steps.length > 0)
  }, [q])

  const total = MODULES.reduce((n, m) => n + (STEPS[m.id] || []).length, 0)
  const shown = groups.reduce((n, g) => n + g.steps.length, 0)

  return (
    <div className="space-y-4">
      {/* header */}
      <div className="flex flex-wrap items-center justify-between gap-3 bg-white border border-gray-200 rounded-xl px-4 py-3 shadow-sm">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-primary-600 to-purple-600 flex items-center justify-center shadow"><LayoutGrid size={16} className="text-white" /></div>
          <div>
            <h1 className="text-base font-bold text-gray-900 leading-tight">Screenshot Gallery</h1>
            <div className="text-[11px] text-gray-500">Every ARS screen in the manual — {total} annotated shots across {MODULES.length} modules</div>
          </div>
        </div>
        <button onClick={() => navigate('/manual/start')} className="inline-flex items-center gap-1.5 text-xs font-semibold text-gray-600 bg-white border border-gray-200 hover:bg-gray-50 rounded-md px-3 py-2"><ArrowLeft size={13} /> Back to manual</button>
      </div>

      {/* search */}
      <div className="flex items-center gap-2 bg-white border border-gray-200 rounded-xl px-3 py-2.5 shadow-sm">
        <div className="relative flex-1">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
          <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search screens by title, module, or caption…"
            className="w-full text-xs border border-gray-200 rounded-md pl-8 pr-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary-300" />
        </div>
        <div className="text-[11px] text-gray-400 whitespace-nowrap">{shown} of {total}</div>
      </div>

      {/* grouped grid */}
      {groups.map(({ mod, steps }) => (
        <div key={mod.id} className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
          <button onClick={() => navigate(`/manual/${mod.id}`)}
            className="w-full flex items-center justify-between px-4 py-2.5 border-b border-gray-100 bg-gray-50/60 hover:bg-gray-50 text-left">
            <span className="text-sm font-bold text-gray-800">{mod.title}</span>
            <span className="text-[11px] text-gray-400">{steps.length} screens · open module →</span>
          </button>
          <div className="p-3 grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3">
            {steps.map(s => (
              <a key={s.n} href={stepImg(mod.id, s)} target="_blank" rel="noreferrer"
                className="group bg-white border border-gray-200 rounded-lg overflow-hidden hover:shadow-md transition-shadow">
                <div className="relative">
                  <img src={stepImg(mod.id, s)} alt={s.title} loading="lazy" className="w-full border-b border-gray-100" />
                  <span className="absolute top-1.5 left-1.5 w-6 h-6 rounded-full bg-red-600 text-white text-[11px] font-bold flex items-center justify-center shadow">{s.n}</span>
                  <span className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 transition-opacity bg-black/60 text-white rounded p-1"><Maximize2 size={12} /></span>
                </div>
                <div className="px-2.5 py-1.5 text-[11px] font-medium text-gray-700 truncate" title={s.title}>{s.title}</div>
              </a>
            ))}
          </div>
        </div>
      ))}
      {groups.length === 0 && <div className="text-sm text-gray-400 py-10 text-center">No screens match “{q}”.</div>}
    </div>
  )
}
