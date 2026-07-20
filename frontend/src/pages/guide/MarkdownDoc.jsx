/**
 * MarkdownDoc — renders an ARS Manual dossier (GitHub-flavored markdown,
 * mermaid fences supported). Renderer recovered from the retired ProcessPage.
 */
import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import mermaid from 'mermaid'

mermaid.initialize({
  startOnLoad: false, theme: 'default', securityLevel: 'loose',
  flowchart: { htmlLabels: true, curve: 'basis' },
  themeVariables: { fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', fontSize: '13px' },
})

function MermaidBlock({ code }) {
  const [svg, setSvg] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => {
    let cancelled = false
    const id = 'm-' + Math.random().toString(36).slice(2, 10)
    mermaid.render(id, code)
      .then(({ svg }) => { if (!cancelled) { setSvg(svg); setErr('') } })
      .catch(e => { if (!cancelled) setErr(String(e?.message || e)) })
    return () => { cancelled = true }
  }, [code])
  if (err) return <pre className="text-xs bg-red-50 text-red-700 p-3 rounded border border-red-200 whitespace-pre-wrap">Mermaid error: {err}</pre>
  return <div className="my-4 p-3 bg-white border border-gray-200 rounded-lg overflow-auto" dangerouslySetInnerHTML={{ __html: svg }} />
}

const slug = (t) => String(t).toLowerCase().replace(/[^\w\s-]/g, '').trim().replace(/\s+/g, '-')

export const mdComponents = {
  pre: ({ children }) => <>{children}</>,
  code({ className, children, ...props }) {
    const lang = /language-(\w+)/.exec(className || '')?.[1]
    const raw = String(children).replace(/\n$/, '')
    if (lang === 'mermaid') return <MermaidBlock code={raw} />
    const isBlock = Boolean(className && className.includes('language-')) || raw.includes('\n')
    if (isBlock) return (
      <pre className="bg-gray-900 text-gray-100 text-xs p-3 rounded-lg overflow-x-auto my-3"><code className={className} {...props}>{raw}</code></pre>
    )
    return <code className="bg-yellow-50 text-red-600 border border-yellow-200 px-1 py-0.5 rounded text-[12px]">{children}</code>
  },
  h1: ({ children }) => <h1 id={slug(children)} className="text-xl font-bold text-gray-900 mt-2 mb-4 pb-2 border-b border-gray-200">{children}</h1>,
  h2: ({ children }) => <h2 id={slug(children)} className="text-lg font-semibold text-gray-900 mt-6 mb-3 pb-1 border-b border-gray-100 scroll-mt-20">{children}</h2>,
  h3: ({ children }) => <h3 id={slug(children)} className="text-sm font-bold text-gray-800 mt-5 mb-2 uppercase tracking-wide scroll-mt-20">{children}</h3>,
  h4: ({ children }) => <h4 className="text-sm font-semibold text-gray-700 mt-4 mb-1">{children}</h4>,
  p:  ({ children }) => <p className="text-[13px] leading-relaxed text-gray-700 mb-3">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-6 space-y-1 text-[13px] text-gray-700 mb-3">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-6 space-y-1 text-[13px] text-gray-700 mb-3">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  blockquote: ({ children }) => <blockquote className="border-l-4 border-primary-400 bg-primary-50/40 px-4 py-2 my-3 text-[13px] text-gray-700 rounded-r">{children}</blockquote>,
  table: ({ children }) => <div className="overflow-x-auto my-4"><table className="min-w-full text-xs border border-gray-200 rounded-lg">{children}</table></div>,
  thead: ({ children }) => <thead className="bg-gray-50 text-gray-700">{children}</thead>,
  th: ({ children }) => <th className="px-3 py-2 text-left font-semibold border-b border-gray-200 whitespace-nowrap">{children}</th>,
  td: ({ children }) => <td className="px-3 py-2 border-b border-gray-100 align-top">{children}</td>,
  a:  ({ href, children }) => <a href={href} className="text-primary-600 hover:underline">{children}</a>,
  hr: () => <hr className="my-6 border-gray-200" />,
  strong: ({ children }) => <strong className="font-semibold text-gray-900">{children}</strong>,
}

/** Split a dossier into named H2 sections; returns { headingLower: bodyMarkdown }. */
export function splitSections(md) {
  const out = {}
  if (!md) return out
  const parts = md.split(/^##\s+/m)
  // parts[0] is the H1 preamble (title) — keep under key '_title'
  out._title = parts[0].trim()
  for (let i = 1; i < parts.length; i++) {
    const nl = parts[i].indexOf('\n')
    const heading = (nl === -1 ? parts[i] : parts[i].slice(0, nl)).trim()
    const body = nl === -1 ? '' : parts[i].slice(nl + 1)
    out[heading.toLowerCase()] = `## ${heading}\n${body}`
  }
  return out
}

export default function MarkdownDoc({ text }) {
  return (
    <div className="max-w-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>{text || ''}</ReactMarkdown>
    </div>
  )
}
