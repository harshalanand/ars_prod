import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate, NavLink } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import mermaid from 'mermaid'
import { FileText, BookOpen, GitBranch, Sliders, Boxes, AlertTriangle, Truck, Layers, ListOrdered, Filter, Shuffle, Waves, CheckCircle2 } from 'lucide-react'

mermaid.initialize({
  startOnLoad: false,
  theme: 'default',
  securityLevel: 'loose',
  flowchart: { htmlLabels: true, curve: 'basis' },
  themeVariables: { fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', fontSize: '13px' },
})

const PROCESS_DOCS = [
  // Start here
  { slug: 'overview',         label: 'Overview',              icon: BookOpen,      desc: 'What ARS does end-to-end',        group: 'Start here' },
  { slug: 'workflow',         label: 'Workflow Chart',        icon: GitBranch,     desc: 'Full pipeline diagram',           group: 'Start here' },
  { slug: 'listing',          label: 'Listing (intro)',       icon: ListOrdered,   desc: 'How OPTs are selected per store', group: 'Start here' },
  // Engine deep-dive (code-level, step by step)
  { slug: 'listing-build',    label: 'Listing Build · Parts 1-5', icon: Filter,    desc: 'listing.py: stock→classify→MBQ',  group: 'Engine deep-dive' },
  { slug: 'stage-a-rank',     label: 'Stage A · Rule & Rank', icon: ListOrdered,   desc: 'R01-R09 gate + OPT priority',     group: 'Engine deep-dive' },
  { slug: 'stage-b-explode',  label: 'Stage B · Explode',     icon: Shuffle,       desc: 'OPT → variant×size, CONT, SZ_MBQ',group: 'Engine deep-dive' },
  { slug: 'stage-c-waterfall',label: 'Stage C · Waterfall',   icon: Waves,         desc: 'The allocation core',             group: 'Engine deep-dive' },
  { slug: 'stage-d-finalize', label: 'Stage D · Finalize',    icon: CheckCircle2,  desc: 'PAK, gates, sec-cap, status',     group: 'Engine deep-dive' },
  // Concepts & reference
  { slug: 'sec-cap',          label: 'Primary & Sec-Cap',     icon: Layers,        desc: 'MJ-grid cap + 130% grid cap',     group: 'Concepts & reference' },
  { slug: 'allocation',       label: 'Allocation (overview)', icon: Boxes,         desc: 'Stages → alloc_header / detail',  group: 'Concepts & reference' },
  { slug: 'pending-alc',      label: 'Pending Allocation',    icon: Truck,         desc: 'PEND lifecycle, DO entry, reco',  group: 'Concepts & reference' },
  { slug: 'fallback',         label: 'Fallback (archived)',   icon: AlertTriangle, desc: 'Removed 2026-05-16 — recipe',     group: 'Concepts & reference' },
  { slug: 'variables',        label: 'Variables Glossary',    icon: Sliders,       desc: 'Every knob and its impact',       group: 'Concepts & reference' },
]

function MermaidBlock({ code }) {
  const ref = useRef(null)
  const [svg, setSvg] = useState('')
  const [err, setErr] = useState('')

  useEffect(() => {
    let cancelled = false
    const id = 'm-' + Math.random().toString(36).slice(2, 10)
    mermaid
      .render(id, code)
      .then(({ svg }) => { if (!cancelled) { setSvg(svg); setErr('') } })
      .catch(e => { if (!cancelled) setErr(String(e?.message || e)) })
    return () => { cancelled = true }
  }, [code])

  if (err) {
    return (
      <pre className="text-xs bg-red-50 text-red-700 p-3 rounded border border-red-200 whitespace-pre-wrap">
        Mermaid error: {err}{'\n\n'}{code}
      </pre>
    )
  }
  return (
    <div
      ref={ref}
      className="my-4 p-3 bg-white border border-gray-200 rounded-lg overflow-auto"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}

function slugifyHeading(text) {
  return String(text)
    .toLowerCase()
    .replace(/[^\w\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
}

const mdComponents = {
  // react-markdown v10 no longer passes an `inline` prop. We unwrap <pre> below and
  // decide inline-vs-block here from the language class / presence of newlines.
  pre: ({ children }) => <>{children}</>,
  code({ className, children, ...props }) {
    const lang = /language-(\w+)/.exec(className || '')?.[1]
    const raw = String(children).replace(/\n$/, '')
    if (lang === 'mermaid') return <MermaidBlock code={raw} />
    const isBlock = Boolean(className && className.includes('language-')) || raw.includes('\n')
    if (isBlock) {
      return (
        <pre className="bg-gray-900 text-gray-100 text-xs p-3 rounded-lg overflow-x-auto my-3">
          <code className={className} {...props}>{raw}</code>
        </pre>
      )
    }
    return <code className="bg-gray-100 text-pink-700 px-1 py-0.5 rounded text-[12px]">{children}</code>
  },
  h1: ({ children }) => {
    const id = slugifyHeading(children)
    return <h1 id={id} className="text-2xl font-bold text-gray-900 mt-6 mb-4 pb-2 border-b border-gray-200">{children}</h1>
  },
  h2: ({ children }) => {
    const id = slugifyHeading(children)
    return <h2 id={id} className="text-xl font-semibold text-gray-900 mt-8 mb-3 pb-1 border-b border-gray-100 scroll-mt-20">{children}</h2>
  },
  h3: ({ children }) => {
    const id = slugifyHeading(children)
    return <h3 id={id} className="text-base font-semibold text-gray-800 mt-5 mb-2 scroll-mt-20">{children}</h3>
  },
  h4: ({ children }) => <h4 className="text-sm font-semibold text-gray-700 mt-4 mb-1">{children}</h4>,
  p:  ({ children }) => <p className="text-sm leading-relaxed text-gray-700 mb-3">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-6 space-y-1 text-sm text-gray-700 mb-3">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-6 space-y-1 text-sm text-gray-700 mb-3">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-4 border-primary-400 bg-primary-50/40 px-4 py-2 my-3 text-sm text-gray-700 italic rounded-r">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto my-4">
      <table className="min-w-full text-xs border border-gray-200 rounded-lg">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-gray-50 text-gray-700">{children}</thead>,
  th: ({ children }) => <th className="px-3 py-2 text-left font-semibold border-b border-gray-200">{children}</th>,
  td: ({ children }) => <td className="px-3 py-2 border-b border-gray-100 align-top">{children}</td>,
  a:  ({ href, children }) => <a href={href} className="text-primary-600 hover:underline" target="_blank" rel="noreferrer">{children}</a>,
  hr: () => <hr className="my-6 border-gray-200" />,
  strong: ({ children }) => <strong className="font-semibold text-gray-900">{children}</strong>,
}

export default function ProcessPage() {
  const { slug } = useParams()
  const navigate = useNavigate()
  const activeSlug = slug || 'overview'

  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    fetch(`/docs/process/${activeSlug}.md`, { cache: 'no-store' })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.text()
      })
      .then(text => {
        if (cancelled) return
        // Guard: if server returned the SPA index.html (404 fallback), show 'not found'
        if (text.trim().startsWith('<!')) throw new Error('Document not found')
        setContent(text)
      })
      .catch(e => { if (!cancelled) setError(String(e?.message || e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [activeSlug])

  const headings = useMemo(() => {
    const out = []
    const re = /^(##\s+|###\s+)(.+)$/gm
    let m
    while ((m = re.exec(content)) !== null) {
      const level = m[1].trim().length // 2 or 3
      const text = m[2].replace(/[`*_]/g, '').trim()
      out.push({ level, text, id: slugifyHeading(text) })
    }
    return out
  }, [content])

  const active = PROCESS_DOCS.find(d => d.slug === activeSlug) || PROCESS_DOCS[0]

  return (
    <div className="flex h-[calc(100vh-80px)] gap-4 p-4 bg-gray-50">
      {/* Left rail: sub-pages */}
      <aside className="w-60 shrink-0 bg-white rounded-xl border border-gray-200 p-3 overflow-y-auto">
        <div className="flex items-center gap-2 px-2 py-2 mb-2">
          <FileText size={16} className="text-primary-600" />
          <div className="text-xs font-bold tracking-wide text-gray-700 uppercase">Process Docs</div>
        </div>
        <nav className="space-y-0.5">
          {PROCESS_DOCS.map((d, i) => {
            const Icon = d.icon
            const isActive = d.slug === activeSlug
            const showGroup = i === 0 || PROCESS_DOCS[i - 1].group !== d.group
            return (
              <div key={d.slug}>
                {showGroup && (
                  <div className="px-2 pt-3 pb-1 text-[9.5px] font-bold uppercase tracking-widest text-gray-400">
                    {d.group}
                  </div>
                )}
                <NavLink
                  to={`/process/${d.slug}`}
                  className={
                    'flex items-start gap-2 px-2.5 py-2 rounded-md text-[12px] transition ' +
                    (isActive
                      ? 'bg-primary-50 text-primary-700 border border-primary-200'
                      : 'text-gray-700 hover:bg-gray-50 border border-transparent')
                  }
                >
                  <Icon size={14} className="mt-[2px] shrink-0" />
                  <div className="leading-tight">
                    <div className="font-semibold">{d.label}</div>
                    <div className="text-[10.5px] text-gray-500">{d.desc}</div>
                  </div>
                </NavLink>
              </div>
            )
          })}
        </nav>
        <div className="mt-3 pt-3 border-t border-gray-100 text-[10px] text-gray-500 px-2 leading-snug">
          Edit these docs in <code className="bg-gray-100 px-1 rounded">frontend/public/docs/process/*.md</code> — they load at runtime.
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 min-w-0 bg-white rounded-xl border border-gray-200 overflow-y-auto">
        <div className="px-6 py-5 border-b border-gray-100 bg-gradient-to-r from-primary-50 to-white sticky top-0 z-10">
          <div className="flex items-center gap-2 text-xs text-gray-500">
            <span>Process</span>
            <span>/</span>
            <span className="text-gray-700 font-medium">{active.label}</span>
          </div>
          <h1 className="text-xl font-bold text-gray-900 mt-1">{active.label}</h1>
          <div className="text-xs text-gray-500 mt-0.5">{active.desc}</div>
        </div>

        <div className="px-6 py-5">
          {loading && <div className="text-sm text-gray-500">Loading…</div>}
          {!loading && error && (
            <div className="p-4 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
              <strong>Could not load doc:</strong> {error}
              <div className="mt-1 text-xs text-amber-700">
                Expected at <code>frontend/public/docs/process/{activeSlug}.md</code>
              </div>
            </div>
          )}
          {!loading && !error && (
            <div className="prose-ars max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                {content}
              </ReactMarkdown>
            </div>
          )}
        </div>
      </main>

      {/* Right rail: on-page TOC */}
      <aside className="w-56 shrink-0 hidden xl:block">
        <div className="sticky top-4 bg-white rounded-xl border border-gray-200 p-3">
          <div className="text-[10px] font-bold tracking-widest text-gray-500 uppercase mb-2 px-1">On this page</div>
          <nav className="space-y-0.5 max-h-[calc(100vh-160px)] overflow-y-auto">
            {headings.length === 0 && <div className="text-[11px] text-gray-400 px-1">No sections</div>}
            {headings.map((h, i) => (
              <a
                key={i}
                href={`#${h.id}`}
                className={
                  'block text-[11px] truncate hover:text-primary-600 px-1.5 py-1 rounded ' +
                  (h.level === 3 ? 'pl-4 text-gray-500' : 'text-gray-700 font-medium')
                }
                title={h.text}
              >
                {h.text}
              </a>
            ))}
          </nav>
        </div>
      </aside>
    </div>
  )
}
