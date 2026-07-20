/**
 * TrainingManualPage — the canonical ARS Manual, in-app.
 * Route /manual/:module. Four tabs per module:
 *   Overview (BRD)  · Rules (FSD + recorded rules)  · Manual (steps)  · Gallery
 * BRD/FSD come from the dossier markdown (public/docs/manual/<module>.md,
 * also read by Claude as memory); Manual/Gallery come from guideSteps + shots.
 */
import { useParams, useNavigate, NavLink, Navigate } from 'react-router-dom'
import { useEffect, useState, useMemo } from 'react'
import {
  BookOpen, ChevronLeft, ChevronRight, Lightbulb, AlertTriangle, MessageSquare,
  FileText, ListChecks, Images, Loader2, Maximize2, LayoutGrid,
} from 'lucide-react'
import { MODULES, STEPS, stepImg } from '@/pages/guide/guideSteps'
import MarkdownDoc, { splitSections } from '@/pages/guide/MarkdownDoc'

const TABS = [
  { key: 'overview', label: 'Overview', sub: 'Business need', icon: FileText },
  { key: 'rules',    label: 'Rules',    sub: 'How & why',     icon: ListChecks },
  { key: 'manual',   label: 'Manual',   sub: 'Step by step',  icon: BookOpen },
  { key: 'gallery',  label: 'Gallery',  sub: 'All screens',   icon: Images },
]

function StepCard({ moduleId, step }) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-100 bg-gradient-to-r from-gray-50 to-white">
        <div className="w-8 h-8 rounded-full bg-red-600 text-white flex items-center justify-center font-extrabold text-sm shrink-0 shadow">{step.n}</div>
        <h3 className="text-sm font-bold text-gray-900">{step.title}</h3>
      </div>
      <a href={stepImg(moduleId, step)} target="_blank" rel="noreferrer" className="block bg-gray-50">
        <img src={stepImg(moduleId, step)} alt={`Step ${step.n} — ${step.title}`} loading="lazy"
          className="w-full border-b border-gray-100 hover:opacity-95 transition-opacity" />
      </a>
      <div className="px-4 py-3 space-y-2">
        <div className="flex items-start gap-2 text-[13px] leading-relaxed text-gray-700">
          <MessageSquare size={14} className="mt-0.5 shrink-0 text-primary-500" />
          <p className="m-0">{step.comment}</p>
        </div>
        {step.tip && <div className="flex items-start gap-2 text-xs leading-relaxed text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2"><Lightbulb size={13} className="mt-0.5 shrink-0 text-emerald-600" /><p className="m-0">{step.tip}</p></div>}
        {step.warn && <div className="flex items-start gap-2 text-xs leading-relaxed text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2"><AlertTriangle size={13} className="mt-0.5 shrink-0 text-amber-600" /><p className="m-0">{step.warn}</p></div>}
      </div>
    </div>
  )
}

export default function TrainingManualPage() {
  const { module } = useParams()
  const navigate = useNavigate()
  const modIdx = MODULES.findIndex(m => m.id === module)
  const [tab, setTab] = useState('overview')
  const [doc, setDoc] = useState({ loading: true, sections: {}, error: '' })

  useEffect(() => { setTab('overview') }, [module])
  useEffect(() => { window.scrollTo(0, 0); document.querySelector('main')?.scrollTo(0, 0) }, [module, tab])

  useEffect(() => {
    if (modIdx === -1) return
    let cancelled = false
    setDoc({ loading: true, sections: {}, error: '' })
    fetch(`/docs/manual/${module}.md`, { cache: 'no-store' })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text() })
      .then(text => {
        if (cancelled) return
        if (text.trim().startsWith('<!')) throw new Error('dossier not found')
        setDoc({ loading: false, sections: splitSections(text), error: '' })
      })
      .catch(e => { if (!cancelled) setDoc({ loading: false, sections: {}, error: String(e.message || e) }) })
    return () => { cancelled = true }
  }, [module, modIdx])

  const { brdMd, rulesMd } = useMemo(() => {
    const s = doc.sections
    const keys = Object.keys(s).filter(k => k !== '_title')
    const brdKeys = keys.filter(k => k.startsWith('brd'))
    const ruleKeys = keys.filter(k => !k.startsWith('brd'))
    return {
      brdMd: [s._title, ...brdKeys.map(k => s[k])].filter(Boolean).join('\n\n'),
      rulesMd: ruleKeys.map(k => s[k]).join('\n\n'),
    }
  }, [doc])

  if (modIdx === -1) return <Navigate to="/manual/start" replace />
  const mod = MODULES[modIdx]
  const steps = STEPS[mod.id] || []
  const prev = MODULES[modIdx - 1]
  const next = MODULES[modIdx + 1]

  const MarkdownTab = ({ md, empty }) => (
    doc.loading ? <div className="text-sm text-gray-400 flex items-center gap-2 py-8 justify-center"><Loader2 size={16} className="animate-spin" /> Loading…</div>
    : doc.error ? <div className="p-4 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">Could not load dossier: {doc.error}<div className="text-xs mt-1">Expected at <code>public/docs/manual/{module}.md</code></div></div>
    : (md && md.trim()) ? <div className="bg-white border border-gray-200 rounded-xl shadow-sm px-5 py-4"><MarkdownDoc text={md} /></div>
    : <div className="text-sm text-gray-400 py-8 text-center">{empty}</div>
  )

  return (
    <div className="flex gap-4 items-start">
      {/* Left rail */}
      <aside className="w-60 shrink-0 sticky top-2 bg-white rounded-xl border border-gray-200 p-3 hidden lg:block">
        <div className="flex items-center gap-2 px-2 py-1.5 mb-1">
          <BookOpen size={15} className="text-primary-600" />
          <div className="text-xs font-bold tracking-wide text-gray-700 uppercase">ARS Manual</div>
        </div>
        <NavLink to="/manual/gallery"
          className="flex items-center gap-2 px-2.5 py-2 mb-1 rounded-md text-[12px] font-semibold text-primary-700 bg-primary-50 border border-primary-200 hover:bg-primary-100">
          <LayoutGrid size={14} /> Screenshot Gallery
        </NavLink>
        <nav className="space-y-0.5">
          {MODULES.map((m, i) => (
            <NavLink key={m.id} to={`/manual/${m.id}`}
              className={({ isActive }) => 'flex items-start gap-2.5 px-2.5 py-2 rounded-md text-[12px] transition border ' +
                (isActive ? 'bg-primary-50 text-primary-700 border-primary-200' : 'text-gray-700 hover:bg-gray-50 border-transparent')}>
              <span className={'w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold shrink-0 mt-[1px] ' +
                (m.id === mod.id ? 'bg-primary-600 text-white' : 'bg-gray-100 text-gray-500')}>{i}</span>
              <span className="leading-tight">
                <span className="font-semibold block">{m.title}</span>
                <span className="text-[10.5px] text-gray-500">{(STEPS[m.id] || []).length} steps · {m.desc}</span>
              </span>
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* Main */}
      <main className="flex-1 min-w-0 space-y-4">
        {/* header + tabs */}
        <div className="bg-white border border-gray-200 rounded-xl shadow-sm overflow-hidden">
          <div className="px-5 py-4">
            <div className="text-[11px] text-gray-400 font-semibold uppercase tracking-wide">ARS Manual · module {modIdx} of {MODULES.length - 1}</div>
            <h1 className="text-lg font-bold text-gray-900 mt-0.5">{mod.title}</h1>
            <div className="text-xs text-gray-500 mt-0.5">{mod.desc}</div>
          </div>
          <div className="flex border-t border-gray-100">
            {TABS.map(t => {
              const Icon = t.icon
              const active = tab === t.key
              const count = t.key === 'manual' || t.key === 'gallery' ? steps.length : null
              return (
                <button key={t.key} onClick={() => setTab(t.key)}
                  className={'flex-1 flex flex-col items-center gap-0.5 py-2.5 text-xs font-semibold border-b-2 transition ' +
                    (active ? 'border-primary-600 text-primary-700 bg-primary-50/40' : 'border-transparent text-gray-500 hover:bg-gray-50')}>
                  <span className="flex items-center gap-1.5"><Icon size={14} /> {t.label}{count != null && <span className="text-[10px] font-bold text-gray-400">({count})</span>}</span>
                  <span className="text-[10px] font-normal text-gray-400">{t.sub}</span>
                </button>
              )
            })}
          </div>
        </div>

        {/* tab body */}
        {tab === 'overview' && <MarkdownTab md={brdMd} empty="No business overview yet for this module." />}
        {tab === 'rules' && <MarkdownTab md={rulesMd} empty="No rules dossier yet for this module." />}
        {tab === 'manual' && (
          steps.length === 0 ? <div className="text-sm text-gray-400 py-8 text-center">No step walkthrough for this module.</div>
          : <div className="space-y-4">{steps.map(s => <StepCard key={s.n} moduleId={mod.id} step={s} />)}</div>
        )}
        {tab === 'gallery' && (
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
            {steps.map(s => (
              <a key={s.n} href={stepImg(mod.id, s)} target="_blank" rel="noreferrer"
                className="group bg-white border border-gray-200 rounded-lg overflow-hidden hover:shadow-md transition-shadow">
                <div className="relative">
                  <img src={stepImg(mod.id, s)} alt={s.title} loading="lazy" className="w-full border-b border-gray-100" />
                  <span className="absolute top-1.5 left-1.5 w-6 h-6 rounded-full bg-red-600 text-white text-[11px] font-bold flex items-center justify-center shadow">{s.n}</span>
                  <span className="absolute top-1.5 right-1.5 opacity-0 group-hover:opacity-100 transition-opacity bg-black/60 text-white rounded p-1"><Maximize2 size={12} /></span>
                </div>
                <div className="px-2.5 py-1.5 text-[11px] font-medium text-gray-700 truncate">{s.title}</div>
              </a>
            ))}
          </div>
        )}

        {/* prev / next */}
        <div className="flex items-center justify-between gap-3 pt-1 pb-8">
          {prev ? <button onClick={() => navigate(`/manual/${prev.id}`)} className="inline-flex items-center gap-1.5 text-xs font-bold text-gray-700 bg-white border border-gray-200 hover:bg-gray-50 rounded-lg px-4 py-2.5"><ChevronLeft size={14} /> {prev.title}</button> : <span />}
          {next ? <button onClick={() => navigate(`/manual/${next.id}`)} className="inline-flex items-center gap-1.5 text-xs font-bold text-white bg-primary-600 hover:bg-primary-700 rounded-lg px-4 py-2.5">{next.title} <ChevronRight size={14} /></button> : <span />}
        </div>
      </main>
    </div>
  )
}
