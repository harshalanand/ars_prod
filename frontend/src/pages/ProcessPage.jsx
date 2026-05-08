import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BookOpen, FileText, Clock, ExternalLink, Search, RefreshCw, AlertTriangle, CheckCircle2, Radio } from 'lucide-react'
import clsx from 'clsx'
import { processAPI } from '@/services/api'

/* ========================================================================
   Minimal markdown → JSX renderer
   Covers: headings (# .. ####), paragraphs, bold/italic, inline code,
   fenced code blocks, ul/ol, tables, hr, blockquotes, links.
   ======================================================================== */

function renderInline(text, keyPrefix = '') {
  const parts = []
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[[^\]]+\]\([^)]+\))/g
  let last = 0, m, i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('`')) {
      parts.push(<code key={`${keyPrefix}-c-${i++}`} className="px-1.5 py-0.5 rounded bg-gray-100 text-[12px] text-rose-700 font-mono">{tok.slice(1, -1)}</code>)
    } else if (tok.startsWith('**')) {
      parts.push(<strong key={`${keyPrefix}-b-${i++}`}>{tok.slice(2, -2)}</strong>)
    } else if (tok.startsWith('*')) {
      parts.push(<em key={`${keyPrefix}-i-${i++}`}>{tok.slice(1, -1)}</em>)
    } else if (tok.startsWith('[')) {
      const mm = /\[([^\]]+)\]\(([^)]+)\)/.exec(tok)
      if (mm) parts.push(<a key={`${keyPrefix}-l-${i++}`} href={mm[2]} className="text-indigo-600 hover:underline" target="_blank" rel="noreferrer">{mm[1]}</a>)
      else parts.push(tok)
    }
    last = m.index + tok.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function renderTable(rows, key) {
  const strip = (line) => {
    const t = line.trim()
    const b1 = t.startsWith('|') ? t.slice(1) : t
    const b2 = b1.endsWith('|') ? b1.slice(0, -1) : b1
    return b2.split('|').map((c) => c.trim())
  }
  const header = strip(rows[0])
  const body = rows.slice(2).map(strip)
  return (
    <div key={key} className="my-4 overflow-x-auto rounded-lg border border-gray-200">
      <table className="min-w-full text-[13px]">
        <thead className="bg-gray-50 text-gray-700">
          <tr>{header.map((h, j) => (
            <th key={j} className="px-3 py-2 text-left font-semibold border-b border-gray-200">{renderInline(h, `th-${key}-${j}`)}</th>
          ))}</tr>
        </thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i} className={i % 2 ? 'bg-gray-50/50' : 'bg-white'}>
              {r.map((c, j) => (
                <td key={j} className="px-3 py-2 align-top border-b border-gray-100 text-gray-800">{renderInline(c, `td-${key}-${i}-${j}`)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function renderMarkdown(md) {
  if (!md) return null
  const lines = md.split('\n')
  const blocks = []
  let i = 0, k = 0
  const push = (el) => blocks.push(<div key={`b-${k++}`}>{el}</div>)

  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) { i++; continue }

    if (line.startsWith('```')) {
      const lang = line.slice(3).trim()
      const buf = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) { buf.push(lines[i]); i++ }
      i++
      push(
        <pre className="my-3 p-3 rounded-lg bg-gray-900 text-gray-100 text-[12.5px] overflow-x-auto font-mono leading-relaxed">
          {lang && <div className="text-[10px] uppercase tracking-wider text-gray-400 mb-2">{lang}</div>}
          <code>{buf.join('\n')}</code>
        </pre>
      )
      continue
    }

    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) {
      const level = h[1].length
      const cls = {
        1: 'text-[22px] font-bold mt-6 mb-3 text-gray-900',
        2: 'text-[18px] font-semibold mt-5 mb-2 text-gray-900 border-b border-gray-100 pb-1',
        3: 'text-[15px] font-semibold mt-4 mb-2 text-gray-800',
        4: 'text-[13px] font-semibold mt-3 mb-1 text-gray-700 uppercase tracking-wide',
      }[level]
      const Tag = `h${level}`
      push(<Tag className={cls} id={`h-${k}`}>{renderInline(h[2], `h-${k}`)}</Tag>)
      i++; continue
    }

    if (/^(-{3,}|_{3,}|\*{3,})\s*$/.test(line)) { push(<hr className="my-4 border-t border-gray-200" />); i++; continue }

    if (line.startsWith('>')) {
      const buf = []
      while (i < lines.length && lines[i].startsWith('>')) { buf.push(lines[i].replace(/^>\s?/, '')); i++ }
      push(<blockquote className="my-3 pl-3 border-l-4 border-indigo-300 text-gray-700 italic">{renderInline(buf.join(' '), `bq-${k}`)}</blockquote>)
      continue
    }

    if (line.trim().startsWith('|') && i + 1 < lines.length && /^\s*\|?\s*[:\-\s|]+\|?\s*$/.test(lines[i + 1])) {
      const buf = []
      while (i < lines.length && lines[i].trim().startsWith('|')) { buf.push(lines[i]); i++ }
      blocks.push(renderTable(buf, `tbl-${k++}`))
      continue
    }

    if (/^[-*+]\s+/.test(line)) {
      const items = []
      while (i < lines.length && /^[-*+]\s+/.test(lines[i])) { items.push(lines[i].replace(/^[-*+]\s+/, '')); i++ }
      push(<ul className="my-3 list-disc pl-6 space-y-1 text-[13.5px] text-gray-800">{items.map((it, j) => <li key={j}>{renderInline(it, `ul-${k}-${j}`)}</li>)}</ul>)
      continue
    }

    if (/^\d+\.\s+/.test(line)) {
      const items = []
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) { items.push(lines[i].replace(/^\d+\.\s+/, '')); i++ }
      push(<ol className="my-3 list-decimal pl-6 space-y-1 text-[13.5px] text-gray-800">{items.map((it, j) => <li key={j}>{renderInline(it, `ol-${k}-${j}`)}</li>)}</ol>)
      continue
    }

    const buf = [line]; i++
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s+|```|>|[-*+]\s+|\d+\.\s+)/.test(lines[i]) && !lines[i].trim().startsWith('|')) {
      buf.push(lines[i]); i++
    }
    push(<p className="my-2.5 text-[13.5px] leading-relaxed text-gray-800">{renderInline(buf.join(' '), `p-${k}`)}</p>)
  }
  return blocks
}

/* ========================================================================
   Helpers
   ======================================================================== */

function formatDate(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) }
  catch { return iso }
}

function formatRelative(iso) {
  if (!iso) return '—'
  const ms = Date.now() - new Date(iso).getTime()
  const s = Math.max(1, Math.round(ms / 1000))
  if (s < 60)    return `${s}s ago`
  if (s < 3600)  return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

function FreshnessBadge({ freshness }) {
  if (!freshness) return null
  const map = {
    fresh:   { bg: 'bg-emerald-50',  text: 'text-emerald-700', label: 'Up-to-date', Icon: CheckCircle2 },
    stale:   { bg: 'bg-amber-50',    text: 'text-amber-800',   label: 'Source changed — review', Icon: AlertTriangle },
    unknown: { bg: 'bg-gray-100',    text: 'text-gray-600',    label: 'Review date unknown', Icon: Clock },
  }
  const cfg = map[freshness.status] || map.unknown
  const Ic = cfg.Icon
  return (
    <span className={clsx('inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10.5px] font-medium', cfg.bg, cfg.text)}>
      <Ic size={11} /> {cfg.label}
    </span>
  )
}

/* ========================================================================
   Page
   ======================================================================== */

const REFRESH_INTERVAL_MS = 30_000  // auto-reload active doc every 30s

export default function ProcessPage() {
  const [docs, setDocs]           = useState([])
  const [staleCount, setStaleCount] = useState(0)
  const [loading, setLoading]     = useState(true)
  const [activeName, setActiveName] = useState(null)
  const [activeDoc, setActiveDoc] = useState(null)
  const [docLoading, setDocLoading] = useState(false)
  const [lastFetched, setLastFetched] = useState(null)
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [search, setSearch] = useState('')
  const refreshTick = useRef(0)

  const loadList = useCallback(async () => {
    try {
      const res = await processAPI.list()
      const list = res?.data?.data || []
      setDocs(list)
      setStaleCount(res?.data?.stale_count || 0)
      return list
    } catch {
      return []
    }
  }, [])

  const loadDoc = useCallback(async (name) => {
    if (!name) return
    setDocLoading(true)
    try {
      const res = await processAPI.get(name)
      setActiveDoc(res?.data?.data || null)
      setLastFetched(new Date().toISOString())
    } catch {
      setActiveDoc(null)
    } finally {
      setDocLoading(false)
    }
  }, [])

  // Initial list
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    loadList()
      .then((list) => { if (!cancelled && list.length && !activeName) setActiveName(list[0].name) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Load current doc whenever selection changes
  useEffect(() => { loadDoc(activeName) }, [activeName, loadDoc])

  // Auto-refresh on interval + on window focus
  useEffect(() => {
    if (!autoRefresh) return

    const tick = async () => {
      refreshTick.current += 1
      // Refresh both list (for stale counts) and current doc
      await loadList()
      if (activeName) await loadDoc(activeName)
    }

    const id = setInterval(tick, REFRESH_INTERVAL_MS)
    const onFocus = () => tick()
    window.addEventListener('focus', onFocus)

    return () => {
      clearInterval(id)
      window.removeEventListener('focus', onFocus)
    }
  }, [autoRefresh, activeName, loadDoc, loadList])

  const grouped = useMemo(() => {
    const q = search.trim().toLowerCase()
    const filtered = q
      ? docs.filter(d =>
          d.title?.toLowerCase().includes(q) ||
          d.category?.toLowerCase().includes(q) ||
          d.name?.toLowerCase().includes(q))
      : docs
    const byCat = {}
    filtered.forEach(d => { (byCat[d.category] ||= []).push(d) })
    return byCat
  }, [docs, search])

  const body = useMemo(() => renderMarkdown(activeDoc?.content), [activeDoc])

  const manualRefresh = async () => {
    setDocLoading(true)
    await loadList()
    await loadDoc(activeName)
  }

  return (
    <div className="flex h-[calc(100vh-48px)] bg-gray-50">
      {/* Sidebar */}
      <aside className="w-72 shrink-0 border-r border-gray-200 bg-white flex flex-col">
        <div className="px-4 py-3 border-b border-gray-200">
          <div className="flex items-center justify-between gap-2 text-gray-800">
            <div className="flex items-center gap-2">
              <BookOpen size={16} className="text-indigo-600" />
              <span className="font-semibold text-[13px]">Process Library</span>
            </div>
            {staleCount > 0 && (
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 text-[10px] font-semibold">
                <AlertTriangle size={10} /> {staleCount} stale
              </span>
            )}
          </div>
          <div className="mt-2 relative">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search processes…"
              className="w-full pl-7 pr-2 py-1.5 text-[12px] rounded border border-gray-200 focus:outline-none focus:ring-1 focus:ring-indigo-400"
            />
          </div>
          <div className="mt-2 flex items-center gap-2 text-[10px] text-gray-500">
            <label className="inline-flex items-center gap-1 cursor-pointer">
              <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} className="rounded" />
              Auto-refresh (30s)
            </label>
            <button onClick={manualRefresh} className="ml-auto inline-flex items-center gap-1 text-indigo-600 hover:underline" title="Refresh now">
              <RefreshCw size={10} /> now
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {loading && <div className="p-3 text-[12px] text-gray-500">Loading…</div>}
          {!loading && docs.length === 0 && <div className="p-3 text-[12px] text-gray-500">No process docs yet.</div>}
          {Object.entries(grouped).map(([cat, items]) => (
            <div key={cat} className="mb-3">
              <div className="px-2 py-1 text-[10px] font-bold text-gray-500 uppercase tracking-wide">{cat}</div>
              <div className="space-y-0.5">
                {items.map(d => {
                  const isStale = d.freshness?.status === 'stale'
                  return (
                    <button
                      key={d.name}
                      onClick={() => setActiveName(d.name)}
                      className={clsx(
                        'w-full flex items-center gap-2 px-2 py-1.5 rounded text-left text-[12px] transition',
                        d.name === activeName ? 'bg-indigo-50 text-indigo-700 font-medium' : 'text-gray-700 hover:bg-gray-100'
                      )}
                    >
                      <FileText size={13} className="shrink-0" />
                      <span className="truncate flex-1">{d.title}</span>
                      {isStale && <AlertTriangle size={10} className="text-amber-600 shrink-0" title="Source changed since last review" />}
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto">
        {docLoading && <div className="p-6 text-[12px] text-gray-500">Loading…</div>}
        {!docLoading && !activeDoc && <div className="p-6 text-[13px] text-gray-500">Select a process from the left.</div>}
        {!docLoading && activeDoc && (
          <article className="max-w-[980px] mx-auto px-8 py-6">
            {/* Meta strip */}
            <div className="flex flex-wrap items-center gap-3 text-[11px] text-gray-500 mb-4">
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-indigo-50 text-indigo-700 font-medium">
                {activeDoc.category}
              </span>
              <FreshnessBadge freshness={activeDoc.freshness} />
              {activeDoc.last_reviewed && (
                <span className="inline-flex items-center gap-1">
                  <Clock size={11} /> Reviewed {activeDoc.last_reviewed}
                </span>
              )}
              <span className="inline-flex items-center gap-1" title="File mtime">
                <Clock size={11} /> File updated {formatDate(activeDoc.file_mtime)}
              </span>
              {activeDoc.source && (
                <span className="inline-flex items-center gap-1">
                  <ExternalLink size={11} /> <code className="text-[11px]">{activeDoc.source}</code>
                </span>
              )}
              <span className="ml-auto inline-flex items-center gap-1 text-[10px]" title={lastFetched ? `Last fetched ${formatDate(lastFetched)}` : ''}>
                <Radio size={11} className={autoRefresh ? 'text-emerald-500 animate-pulse' : 'text-gray-400'} />
                {autoRefresh ? 'Live' : 'Paused'} · {lastFetched ? formatRelative(lastFetched) : '—'}
              </span>
            </div>

            {/* Stale banner */}
            {activeDoc.freshness?.status === 'stale' && activeDoc.freshness?.stale_files?.length > 0 && (
              <div className="mb-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-[12px] text-amber-900">
                <div className="flex items-center gap-2 font-semibold mb-1">
                  <AlertTriangle size={13} /> This doc may be out-of-date
                </div>
                <div>
                  The following source file(s) changed after the last review ({activeDoc.last_reviewed || 'no date'}):
                </div>
                <ul className="mt-1 ml-4 list-disc space-y-0.5">
                  {activeDoc.freshness.stale_files.map((f) => (
                    <li key={f.path}><code className="text-[11px]">{f.path}</code> — modified {formatDate(f.mtime)}</li>
                  ))}
                </ul>
                <div className="mt-2 text-[11px]">
                  Open the file at <code className="text-[11px]">backend/app/docs/processes/{activeDoc.name}.md</code>, update the affected section, and bump <code>last_reviewed</code>.
                </div>
              </div>
            )}

            <h1 className="text-[26px] font-bold text-gray-900 mb-2">{activeDoc.title}</h1>
            <div className="h-px bg-gray-200 mb-4" />

            <div className="prose max-w-none">
              {body}
            </div>

            {(activeDoc.directives_resolved > 0) && (
              <div className="mt-6 text-[10.5px] text-gray-500">
                <Radio size={10} className="inline text-emerald-500" /> {activeDoc.directives_resolved} live data block(s) rendered from current DB / source.
              </div>
            )}

            <div className="mt-10 p-3 rounded-lg bg-amber-50 border border-amber-200 text-[12px] text-amber-900">
              <strong>Editing:</strong> this page is served from
              <code className="mx-1 px-1 bg-amber-100 rounded">backend/app/docs/processes/{activeDoc.name}.md</code>.
              Edit that file and changes appear within ~30s (auto-refresh) or on page reload. Bump <code>last_reviewed</code> to clear the stale badge.
            </div>
          </article>
        )}
      </main>
    </div>
  )
}
